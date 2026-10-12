# -*- coding: utf-8 -*-
"""bot_ops 验收的纯工具（普通模块，非测试文件）。

只放"怎么表达场景"的辅助：写造物、读状态、收缩超时常量、轮询 job。
不含任何对被测实现的引用（除 tighten_constant 需要按契约去找超时常量）。
"""
import json
import socket
import stat as _stat
import sys
import time
from pathlib import Path

import pytest
import yaml

FAKE_TOKEN = "123456789:test-AAaaBBccDDddEEeeFFggHHhh11"
FAKE_TOKEN_SAME_HEAD = "123456789:test-ZZzzYYyyXXxxWWwwVVvvUUuuTTss22"   # 数字段与上面相同
FAKE_TOKEN_OTHER_HEAD = "987654321:test-MMmmNNnnOOooPPppQQqqRRrrSSss33"


def dsh_bot(cfg_dir, bot_id="xiaoyu", **fields):
    """在新系统配置根造一只 bot（dsh 来源）。返回 yml 路径。"""
    fields.setdefault("display_name", bot_id)
    fields.setdefault("dispatcher_port", 17951)
    return write_bot_yml(cfg_dir, bot_id, **fields)


def free_port():
    """拿一个当前空闲端口号并立刻释放。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def write_bot_yml(dirpath, bot_id, **fields):
    """在配置根下写一份 `<bot_id>.yml`（值是标量）。返回路径。"""
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    lines = ["id: %s" % bot_id]
    for k in sorted(fields):
        v = fields[k]
        if isinstance(v, bool):
            v = "true" if v else "false"
        lines.append("%s: %s" % (k, v))
    p = d / ("%s.yml" % bot_id)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def read_yaml(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def mode_of(path):
    """POSIX 权限位（只取低 9 位）。Windows 上语义不可靠，用例自行 skip。"""
    return _stat.S_IMODE(Path(path).stat().st_mode)


def snapshot_tree(root):
    """递归快照一个目录（相对路径 -> 类型/大小/纳秒 mtime），用于「零字节产出」断言。"""
    root = Path(root)
    if not root.exists():
        return None
    out = {}
    for p in sorted(root.rglob("*")):
        try:
            st = p.stat()
            out[str(p.relative_to(root))] = (p.is_dir(), st.st_size, st.st_mtime_ns)
        except OSError:
            out[str(p.relative_to(root))] = ("gone",)
    return out


def tree_bytes(root):
    """递归读一个目录：相对路径 -> 文件字节（目录记 None）。用于「逐字节没变」断言。

    目录不存在返回 None。读不出来的文件记成 ("unreadable", 权限位)，照样能比。
    """
    root = Path(root)
    if not root.exists():
        return None
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_dir():
            out[rel] = None
            continue
        try:
            out[rel] = p.read_bytes()
        except OSError:
            out[rel] = ("unreadable", _stat.S_IMODE(p.stat().st_mode))
    return out


#: §4b 第 3 格与 §10 的 404 共用的那句 detail（同一道放行闸、同一句话）
GATE_404_DETAIL = "没有这个新系统的 bot（这一页只管新系统的 bot）"


def freeze_roots(*roots):
    """给几个目录各拍一张快照：文件字节 + 每个条目的（类型, 大小, 纳秒 mtime）+ 根自己的 mtime。

    字节用来断言「逐字节没变」；mtime 用来抓「内容一样但被重写过」与「建了又删的临时文件」
    （原子写会动目录的 mtime）。目录不存在也记得住（之后冒出来算变了）。
    """
    out = []
    for r in roots:
        r = Path(r)
        own = r.stat().st_mtime_ns if r.exists() else None
        out.append((r, tree_bytes(r), snapshot_tree(r), own))
    return out


def assert_roots_untouched(frozen):
    for root, data, meta, own in frozen:
        assert tree_bytes(root) == data, "%s 下的文件字节变了（或多了、少了文件）" % root
        assert snapshot_tree(root) == meta, "%s 下有文件被重写过（字节一样，mtime 变了）" % root
        now = root.stat().st_mtime_ns if root.exists() else None
        assert now == own, "%s 这个目录被写过（建过或删过文件）" % root


def assert_no_job_left(client, monkeypatch, tmp):
    """「没有后台任务产生」的可观察面：此刻再发一次重启全部，必须被受理。

    重启全部、单只重启、开关同步共用一把互斥（§4b）。前一个请求要是偷偷起了任务且还在跑，
    这里会拿到 409 restart_in_progress。探测走覆盖命令：设了 HUB_RESTART_CMD 可用性恒真（§1），
    整段换成执行这条命令、不发任何 launchctl（§3），所以不依赖平台与配置根，也不会弄脏调用方
    对假 launchctl「零调用」的断言。探测自己起的那个任务等到终态才返回。
    """
    probe = Path(tmp) / "mutex-probe.txt"
    monkeypatch.setenv("BOTOPS_PROBE", str(probe))
    monkeypatch.setenv("HUB_RESTART_CMD",
                       python_cmd("import os;open(os.environ['BOTOPS_PROBE'],'w').write('probe')"))
    try:
        r = client.post("/hub/api/bots/restart")
        body = r.get_json()
        assert r.status_code == 202, (
            "前一个请求留下了还在跑的后台任务（互斥被占着）：%s %r" % (r.status_code, body))
        st = wait_job(client, body["job_id"])
        assert st["state"] == "done", st
    finally:
        monkeypatch.delenv("HUB_RESTART_CMD", raising=False)


def assert_gate_rejected(resp, frozen, launchctl, client, monkeypatch, tmp, calls_before=0):
    """没过放行闸（§0 第 2 条）的请求，四件事一起成立。

    ① 404 + 错误体逐字；② 快照里的配置根逐字节没变、也没被写过；
    ③ 假 launchctl 零调用；④ 没有后台任务产生。

    `calls_before`：发这个请求之前假 launchctl 已有的调用条数（场景里先做过别的动作时用），
    「零调用」指的是这之后一条都没多。
    """
    assert resp.status_code == 404, (resp.status_code, resp.get_json())
    assert resp.get_json() == {"error": "bot_not_found", "detail": GATE_404_DETAIL}, resp.get_json()
    assert_roots_untouched(frozen)
    extra = launchctl.calls()[calls_before:]
    assert extra == [], ("被拒了还调了 launchctl", extra)
    assert_no_job_left(client, monkeypatch, tmp)
    extra = launchctl.calls()[calls_before:]
    assert extra == [], ("被拒之后冒出了 launchctl 调用", extra)
    assert_roots_untouched(frozen)


def heartbeat_path(dsh_home, bot):
    return Path(dsh_home) / "bots" / bot / "state" / "heartbeat"


def write_heartbeat(path, pid=4242, at_ms=None):
    """写一份心跳。`at` 是毫秒（契约 §5）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    at = int(time.time() * 1000) if at_ms is None else int(at_ms)
    path.write_text(json.dumps({"at": at, "pid": pid}), encoding="utf-8")


def python_cmd(code):
    """跨平台覆盖命令串：解释器路径与代码都用双引号包住。

    §5b 说覆盖命令经 `shlex.split`（默认 posix 模式）：未加引号时反斜杠是转义符，
    Windows 路径会被吃掉；双引号内反斜杠只在 `$ ` " \\ 换行` 前有特殊含义，
    所以这样写的路径能原样还原。代码里用单引号取环境变量、避免再嵌引号。
    """
    return '"%s" -c "%s"' % (sys.executable, code)


def wait_job(client, job_id, timeout=20.0):
    """轮询 §9 的 job 端点直到终态，返回终态体（四键）。"""
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        r = client.get("/hub/api/bots/restart/%s" % job_id)
        assert r.status_code == 200, (r.status_code, r.get_json())
        body = r.get_json()
        assert set(body) == {"state", "exit_code", "took_ms", "tail"}, body
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("job %s 没在 %.1f 秒内进终态：%r" % (job_id, timeout, body))


def finish_sync(client, body, timeout=20.0):
    """开关（§4b）的 200 体收尾：把 `sync` 指到的后台任务等到终态。

    `started` 与 `busy` 都带 job_id（后者是别人正在跑的那只），一样等；`skipped` 没有任务。
    返回终态体（没有任务时返回 None）。
    """
    sync = (body or {}).get("sync") or {}
    job_id = sync.get("job_id")
    if not job_id:
        return None
    return wait_job(client, job_id, timeout=timeout)


def _candidate_modules():
    """可能放超时常量的模块（契约 §5b 点名 bot_launchd，bots_client 兜底）。"""
    mods = []
    try:
        from moments import bot_launchd  # noqa: F401
        mods.append(bot_launchd)
    except ImportError:
        pass
    from moments import bots_client
    mods.append(bots_client)
    return mods


def require_bot_launchd():
    """契约 §5：bot_launchd 提供唯一实现。拿不到就直接红，别让用例去猜。"""
    try:
        from moments import bot_launchd
        return bot_launchd
    except ImportError:
        pytest.fail("moments.bot_launchd 不存在 —— INTERFACE §5 说它提供唯一实现")


def tighten_constant(monkeypatch, *, name=None, value=None, new=0.5):
    """把契约写明"测试可改"的超时常量改小。

    - `name` 给了就按名字找（跨候选模块）；
    - 否则按值找（契约给了 45/30 这类数值，但没给常量名）。
    找不到直接 fail：契约说这些常量是可改的模块常量，找不到就是没对齐。
    """
    hits = []
    for mod in _candidate_modules():
        for attr in dir(mod):
            try:
                v = getattr(mod, attr)
            except Exception:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if (name is not None and attr == name) or (name is None and v == value):
                hits.append((mod, attr))
    if not hits:
        pytest.fail("没找到可改的超时常量（name=%r value=%r）。INTERFACE §1 说超时常量"
                    "（心跳 15 秒 / 重启等待 45 秒 / 新建探活 30 秒 / launchctl 10 秒）"
                    "都是可改的模块常量——先确认实现把它定义在 bot_launchd 或 bots_client 里。"
                    % (name, value))
    for mod, attr in hits:
        monkeypatch.setattr(mod, attr, new)
    return hits
