"""需求⑤（公开 voicecall/server.py、moments/web.py、moments/hub_addbot.py）：
停用 bot 的生产者入口不 POST /ensure_worker、不写 inbox；set_enabled 幂等且不调 launchctl。

2026-10-12 同步：`INTERFACE-新系统bot运维.md` 第 6 稿把开关收成「只对新系统的 bot 开放」，放行闸
同时管着 HTTP 端点与这里直接调的写盘入口 set_enabled（§0 第 2 条、§4b、§5b 第 4 条）。本文件
set_enabled 那三条原先把被测 bot 造在旧系统配置根（HUB_CONFIGS_DIR）下，现在会在入口被拒、写盘
不发生。改法是把被测 bot 造到新系统配置根（HUB_CONFIGS_DSH_DIR）下：幂等、恢复、不调 launchctl
三件事的预期一个字没改，只换了环境准备。旧系统的 bot 直接调写盘入口被拒那一面，另补了反向用例。
"""
import importlib
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from _helpers import write_cfg


class _Rec(BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):
        _Rec.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    do_GET = do_POST

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Rec.hits.clear()
    srv = HTTPServer(("127.0.0.1", 0), _Rec)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _cfg(iso, bot, enabled, port, cfg_dir=None):
    """造一只 bot：配置 + 频道目录。`cfg_dir` 不给就落在旧系统配置根，给了就落在那个配置根下。"""
    write_cfg(cfg_dir or iso["cfg"], bot, enabled=enabled,
              extra=f"bot_channel_path: {iso['home']}/.claude/channels/{bot}\ndispatcher_port: {port}\nchat_id: 100\n")
    d = os.path.join(iso["home"], ".claude", "channels", bot)
    os.makedirs(os.path.join(d, "inbox"), exist_ok=True)
    with open(os.path.join(d, "access.json"), "w") as f:
        f.write('{"allowFrom": [100], "groups": {}}')
    return d


def _inbox(d):
    return os.listdir(os.path.join(d, "inbox"))


def _vc(registry):
    import voicecall.server as s
    return importlib.reload(s)


def _mw(registry):
    import moments.web as w
    return importlib.reload(w)


# ---------- voicecall ----------
def test_voicecall_write_inbox_停用bot_返回False_不写文件_stderr一行(iso, registry, server, capsys):
    d = _cfg(iso, "bot2", False, server.server_port)
    assert _vc(registry)._write_inbox("bot2", "合成文本", "voice-") is False
    assert _inbox(d) == []
    assert re.search(r"^\[\S+\] bot2 stopped, skip$", capsys.readouterr().err, re.M)


# 注：voicecall 启用路径的配置来源（chat_id/dispatcher_port 从哪读）契约未给，临时 configs 下报"未配 chat_id"，
# 故不写启用侧正向对照；停用侧靠 stderr 的 "stopped, skip" 行区分。


def test_voicecall_ensure_worker_停用bot_不POST_stderr一行(iso, registry, server, capsys):
    _cfg(iso, "bot2", False, server.server_port)
    assert _vc(registry)._ensure_worker_alive("bot2") is None
    assert _Rec.hits == []
    assert not os.path.exists(iso["calls"])
    assert re.search(r"^\[\S+\] bot2 stopped, skip$", capsys.readouterr().err, re.M)


def test_voicecall_中文bot名停用_不写文件(iso, registry, server):
    d = _cfg(iso, "陈璐璐", False, server.server_port)
    assert _vc(registry)._write_inbox("陈璐璐", "合成", "voice-") is False and _inbox(d) == []


# ---------- moments/web ----------
def test_moments_web_ensure_worker_停用bot_不POST_不写inbox_stderr一行(iso, registry, server, capsys):
    d = _cfg(iso, "bot3", False, server.server_port)
    assert _mw(registry)._ensure_worker_alive("bot3", "100", d) is None
    assert _Rec.hits == [] and _inbox(d) == []
    assert re.search(r"^\[\S+\] bot3 stopped, skip$", capsys.readouterr().err, re.M)


def test_moments_web_ensure_worker_启用bot_有POST_正向对照(iso, registry, server):
    d = _cfg(iso, "bot3", True, server.server_port)
    _mw(registry)._ensure_worker_alive("bot3", "100", d)
    assert any(p.rstrip("/").endswith("/ensure_worker") for p in _Rec.hits), _Rec.hits


# ---------- hub_addbot.set_enabled ----------
def _hub(registry):
    import moments.hub_addbot as h
    return importlib.reload(h)


#: 三个都是假名。这台机器上真有 bot2、bot4 等，会走到写盘或 launchctl 流程的用例不用真名。
_NEW = "xinbot"      # 只在新系统配置根下
_OLD = "jiubot"      # 只在旧系统配置根下
_TWIN = "twinbot"    # 新旧两个配置根下同名


@pytest.fixture
def dsh(iso, tmp_path, monkeypatch):
    """新系统那一面，全部落在本用例的 tmp_path 下（INTERFACE-新系统bot运维 §1 的接缝）。

    - HUB_CONFIGS_DSH_DIR：新系统配置根。开关的写盘目标恒在这里（§4b）。
    - DSH_BOT_HOME、HUB_LAUNCHAGENTS_DIR：一并指到 tmp，不留给默认值。
    - HUB_LAUNCHCTL：换成只记账的假 launchctl。iso 夹具在 PATH 上放的拒绝桩看不见经这个接缝的
      调用，「不调 launchctl」两条路都要盯住。假件把记账路径写死在脚本里（不靠子进程的环境变量），
      记完以 97 退出，绝不转发给真实命令。
    - HUB_BOTS_FILE、HUB_RESTART_CMD：清掉。前者设了来源判定为空、一律 404；后者设了会被当命令执行。

    要排在 registry 夹具前面（用例签名里写在它左边）：注册表重载时环境已经就位。
    """
    cfg, home, agents = tmp_path / "dsh-cfg", tmp_path / "dsh-home", tmp_path / "dsh-LaunchAgents"
    for d in (cfg, home, agents):
        d.mkdir()
    calls_log = tmp_path / "hub-launchctl-calls.log"
    fake = tmp_path / "hub-launchctl" / "launchctl"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n"
                    "# desync 验收的假 launchctl：只记账，然后以 97 退出；绝不转发给真实命令。\n"
                    "printf '%%s\\n' \"$*\" >> '%s'\n"
                    "echo 'test_mode: fake HUB_LAUNCHCTL called' >&2\n"
                    "exit 97\n" % calls_log, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(cfg))
    monkeypatch.setenv("DSH_BOT_HOME", str(home))
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(agents))
    monkeypatch.setenv("HUB_LAUNCHCTL", str(fake))
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    monkeypatch.delenv("HUB_RESTART_CMD", raising=False)
    return {"cfg": str(cfg), "home": str(home), "calls": str(calls_log)}


def _enabled_in_yml(cfg_dir, bot):
    import yaml
    with open(os.path.join(cfg_dir, f"{bot}.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f).get("enabled")


def _yml_bytes(cfg_dir, bot):
    with open(os.path.join(cfg_dir, f"{bot}.yml"), "rb") as f:
        return f.read()


def _launchctl_calls(iso, dsh):
    """两条路上的 launchctl 调用记录拼在一起：PATH 上的拒绝桩（iso）与 HUB_LAUNCHCTL 假件（dsh）。"""
    out = ""
    for p in (iso["calls"], dsh["calls"]):
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                out += f.read()
    return out


def _freeze(*roots):
    """给几个配置根各拍一张快照：每个条目的字节与纳秒 mtime，外加目录自己的 mtime。

    字节抓「内容被改、多了少了文件」；条目 mtime 抓「内容一样但被重写过」；目录 mtime 抓
    「建了又删的临时文件」（原子写会先在同目录落一个临时文件）。
    """
    snap = []
    for root in roots:
        entries = {}
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            data = None
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    data = f.read()
            entries[name] = (data, os.lstat(p).st_mtime_ns)
        snap.append((root, entries, os.stat(root).st_mtime_ns))
    return snap


def _trap_subprocess(monkeypatch):
    """把 subprocess 的五个入口换成「记一笔再抛错」。返回那本账（列表）。

    记账而不是只抛错：被测代码要是把异常吞了，或者这次调用本来就预期抛异常，光抛错看不出来。
    """
    spawned = []

    def boom(*a, **k):
        spawned.append(a[0] if a else k.get("args"))
        raise AssertionError("subprocess 被调用（被拒的开关不许起任何子进程）")
    for n in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, n, boom)
    return spawned


# -- 新系统的 bot：写盘入口照常工作（§4b 第 6 格：行级编辑、原子写、同值重复提交幂等）--

def test_set_enabled_False两次_幂等_响应相同_yml为false(iso, dsh, registry):
    _cfg(iso, _NEW, True, 1, cfg_dir=dsh["cfg"])
    h = _hub(registry)
    assert _NEW not in registry.disabled_ids_safe()      # 起点：注册表看得见它，且它没停
    r1 = h.set_enabled(_NEW, False)
    once = _yml_bytes(dsh["cfg"], _NEW)
    r2 = h.set_enabled(_NEW, False)
    assert r1 == r2 and _enabled_in_yml(dsh["cfg"], _NEW) is False
    assert _yml_bytes(dsh["cfg"], _NEW) == once, "同值再提交一次，配置文件的字节变了"
    assert _NEW in registry.disabled_ids_safe()


def test_set_enabled_True_恢复_不调launchctl(iso, dsh, registry):
    _cfg(iso, _NEW, False, 1, cfg_dir=dsh["cfg"])
    h = _hub(registry)
    assert _NEW in registry.disabled_ids_safe()          # 起点：它确实在停止集合里，下面的「不在」才有分量
    h.set_enabled(_NEW, True)
    assert _enabled_in_yml(dsh["cfg"], _NEW) is True
    assert _NEW not in registry.disabled_ids_safe()
    assert not os.path.exists(iso["calls"]), open(iso["calls"]).read() if os.path.exists(iso["calls"]) else ""
    assert _launchctl_calls(iso, dsh) == "", "写盘入口调了 launchctl（经 HUB_LAUNCHCTL 接缝）"


def test_set_enabled_True两次_幂等_yml仍为true(iso, dsh, registry):
    _cfg(iso, _NEW, False, 1, cfg_dir=dsh["cfg"])
    h = _hub(registry)
    r1 = h.set_enabled(_NEW, True)
    once = _yml_bytes(dsh["cfg"], _NEW)
    r2 = h.set_enabled(_NEW, True)
    assert r1 == r2
    assert _enabled_in_yml(dsh["cfg"], _NEW) is True
    assert _yml_bytes(dsh["cfg"], _NEW) == once, "同值再提交一次，配置文件的字节变了"


def test_set_enabled_新系统的bot_写盘只落在新系统配置根_旧系统配置根原样不动(iso, dsh, registry):
    """§0 第 2 条：写入目标恒在新系统这边，旧系统配置根下的任何文件都不是写入目标。

    旁边放一只不相干的旧系统 bot 当对照：它的文件不许被重写，旧系统配置根下也不许冒出新文件。
    """
    _cfg(iso, _OLD, True, 2)
    _cfg(iso, _NEW, True, 1, cfg_dir=dsh["cfg"])
    h = _hub(registry)
    legacy_before = _freeze(iso["cfg"])
    h.set_enabled(_NEW, False)
    assert _enabled_in_yml(dsh["cfg"], _NEW) is False
    assert _freeze(iso["cfg"]) == legacy_before, "对新系统的 bot 写盘，旧系统配置根被动过"


# -- 旧系统的 bot：直接调写盘入口同样被拒（§0 第 2 条：不写盘、不调 launchctl、不起子进程、不登记 job）--
# HTTP 那一层的 404 在同目录 test_hub_enabled.py；这里守的是「绕过路由直接调函数也过同一道闸」。
# 被拒的形态：函数不正常返回，抛出的异常带着契约里的错误码 bot_not_found。异常的类名不在接口约定里，
# 不点名，只认错误码出现在异常文本里。

def _assert_rejected_untouched(iso, dsh, registry, monkeypatch, h, bot, flag):
    frozen = _freeze(iso["cfg"], dsh["cfg"])
    stopped = set(registry.disabled_ids_safe())
    spawned = _trap_subprocess(monkeypatch)
    threads = threading.active_count()
    with pytest.raises(Exception) as ei:
        h.set_enabled(bot, flag)
    assert "bot_not_found" in str(ei.value), "被拒的原因不是 bot_not_found：%r" % (ei.value,)
    assert spawned == [], "被拒了还起了子进程：%r" % (spawned,)
    assert _freeze(iso["cfg"], dsh["cfg"]) == frozen, "被拒了，配置根却被动过（字节、mtime 或多了少了文件）"
    assert set(registry.disabled_ids_safe()) == stopped, "被拒了，停止集合却变了"
    assert _launchctl_calls(iso, dsh) == "", "被拒了还调了 launchctl"
    assert threading.active_count() <= threads, "被拒了还起了后台线程"


@pytest.mark.parametrize("flag", [False, True], ids=["disable", "enable"])     # 请求停用 / 请求启用
def test_set_enabled_只在旧系统配置根下的bot_被拒_配置逐字节不变_零launchctl(iso, dsh, registry, monkeypatch, flag):
    _cfg(iso, _OLD, not flag, 1)
    _assert_rejected_untouched(iso, dsh, registry, monkeypatch, _hub(registry), _OLD, flag)
    assert _enabled_in_yml(iso["cfg"], _OLD) is (not flag)


@pytest.mark.parametrize("flag", [False, True], ids=["disable", "enable"])     # 请求停用 / 请求启用
def test_set_enabled_新旧同名_生效的是旧系统那份_被拒_两份都逐字节不变(iso, dsh, registry, monkeypatch, flag):
    """新系统那份停着、旧系统那份启用 → 生效的是旧系统那份，按旧系统的 bot 算（§4b 第 3 格那张表）。

    新系统配置根下确实有它的文件，「有文件就写」的实现会在这里漏过去：请求启用时尤其要紧，
    写进去之后生效的那份就变成新系统的了，等于把这只 bot 从旧系统手里抢过来。
    """
    _cfg(iso, _TWIN, True, 1)
    _cfg(iso, _TWIN, False, 1, cfg_dir=dsh["cfg"])
    _assert_rejected_untouched(iso, dsh, registry, monkeypatch, _hub(registry), _TWIN, flag)
    assert _enabled_in_yml(iso["cfg"], _TWIN) is True and _enabled_in_yml(dsh["cfg"], _TWIN) is False
