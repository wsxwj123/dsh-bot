# -*- coding: utf-8 -*-
"""daily-wildcard-job 验收：逐条对照 .devflow/daily-wildcard-job-接口.md 的 7 条行为清单与 3 条错误契约。

观察面只有三个，全部是对外入口：
  1. 子进程运行 scripts/daily_wildcards.py 的退出码与输出；
  2. 临时 BOTLIFE_STATE_DB 里 daily_wildcards 表的记录（sqlite3 直查）；
  3. 假 deepseek_client 记录下来的 LLM 调用内容（提示词）。

不 import 被测模块，不 mock 内部层。DeepSeek 用 PYTHONPATH 上的同名假模块顶替，
并把 http(s)_proxy 指到必然拒绝连接的回环地址，保证不连外网。
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "daily_wildcards.py"

# 假 deepseek_client：call_json 返回固定 JSON 数组（5 张卡），或按 FAKE_DW_MODE 返回
# 空/非数组/抛错；每次调用把入参追加写到 FAKE_DW_DUMP，供提示词断言。
_FAKE_MODULE = '''# -*- coding: utf-8 -*-
import json
import os

_calls = 0


def _five():
    return [
        {"text": "今天想去书店坐一会儿，顺便看看新到的散文。",
         "content": "今天想去书店坐一会儿，顺便看看新到的散文。",
         "title": "去书店", "type": "daily", "category": "日常"},
        {"text": "晚饭后想沿着河边慢慢走一圈。",
         "content": "晚饭后想沿着河边慢慢走一圈。",
         "title": "河边散步", "type": "daily", "category": "日常"},
        {"text": "翻到一张旧照片，忽然想起很久没联系的朋友。",
         "content": "翻到一张旧照片，忽然想起很久没联系的朋友。",
         "title": "旧照片", "type": "daily", "category": "回忆"},
        {"text": "想学做一道没试过的菜，周末去菜场挑材料。",
         "content": "想学做一道没试过的菜，周末去菜场挑材料。",
         "title": "学做菜", "type": "daily", "category": "日常"},
        {"text": "最近总在听同一首歌，想找找它背后的故事。",
         "content": "最近总在听同一首歌，想找找它背后的故事。",
         "title": "单曲循环", "type": "daily", "category": "音乐"},
    ]


def call_json(*args, **kwargs):
    global _calls
    _calls += 1
    dump = os.environ.get("FAKE_DW_DUMP")
    if dump:
        with open(dump, "a", encoding="utf-8") as f:
            f.write(json.dumps({"args": args, "kwargs": kwargs},
                               ensure_ascii=False, default=str) + "\\n")
    mode = os.environ.get("FAKE_DW_MODE", "five")
    if mode == "fail_all":
        raise RuntimeError("fake deepseek api error")
    if mode == "fail_key":
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    if mode == "fail_first" and _calls == 1:
        raise RuntimeError("fake deepseek api error")
    if mode == "empty":
        return []
    if mode == "dict":
        return {"cards": _five()}
    return _five()
'''


def _require_script():
    """脚本不存在时让每条用例在做任何事之前明确失败。

    尤其防止“BOTLIFE_STATE_DB 未设应非 0 退出”那条把“python 打不开脚本”误判为通过。
    """
    assert SCRIPT.is_file(), "被测脚本不存在：%s" % SCRIPT


@pytest.fixture
def fake_llm(tmp_path):
    """在独立的目录里放一个假 deepseek_client 模块，返回 (模块目录, 调用记录文件)。"""
    mod_dir = tmp_path / "fake_llm"
    mod_dir.mkdir()
    (mod_dir / "deepseek_client.py").write_text(_FAKE_MODULE, encoding="utf-8")
    return mod_dir, tmp_path / "llm_calls.jsonl"


def _write_bot(tmp_path, bot_id, enabled=True, persona="慢热的人，喜欢书和散步。"):
    """按现有新系统测试的布局写 configs/<bot_id>.yml 与频道目录。"""
    cfgs = tmp_path / "configs"
    cfgs.mkdir(exist_ok=True)
    channel = tmp_path / "bots" / bot_id / "channel"
    channel.mkdir(parents=True, exist_ok=True)
    (tmp_path / "channels").mkdir(exist_ok=True)
    (tmp_path / "channels" / bot_id).mkdir(exist_ok=True)
    lines = [
        "id: %s" % bot_id,
        "chat_id: '1'",
        "bot_channel_path: %s" % json.dumps(str(channel)),
        "dispatcher_port: 0",
        "persona_summary: %s" % json.dumps(persona, ensure_ascii=False),
    ]
    if enabled is not None:
        lines.append("enabled: " + ("true" if enabled else "false"))
    (cfgs / ("%s.yml" % bot_id)).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_env(tmp_path, fake_llm, mode="five", api_key="sk-test-daily-wildcards-0000"):
    """给子进程一套全新的临时环境；api_key 为 None 表示密钥缺失。"""
    mod_dir, dump = fake_llm
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    dsh_home = tmp_path / "dsh-home"
    dsh_home.mkdir(exist_ok=True)
    refs = "  DEEPSEEK_API_KEY: %s\n" % api_key if api_key else "  {}\n"
    (dsh_home / "credentials.yaml").write_text(
        "version: 1\nrefs:\n" + refs, encoding="utf-8")

    env = {k: v for k, v in os.environ.items()
           if k not in ("BOTLIFE_STATE_DB", "DEEPSEEK_API_KEY", "PYTHONPATH")}
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "DSH_BOT_HOME": str(dsh_home),
        # 新系统配置根；两个变量指同一处，覆盖两种读配置的口径
        "HUB_CONFIGS_DIR": str(tmp_path / "configs"),
        "HUB_CONFIGS_DSH_DIR": str(tmp_path / "configs"),
        "HUB_CHANNELS_DIR": str(tmp_path / "channels"),
        "HUB_LAUNCHAGENTS_DIR": str(tmp_path / "LaunchAgents"),
        "PYTHONPATH": str(mod_dir),
        "PYTHONIOENCODING": "utf-8",
        "FAKE_DW_MODE": mode,
        "FAKE_DW_DUMP": str(dump),
        # 兜底断网：任何漏到真实 HTTP 客户端的调用都会连接失败
        "http_proxy": "http://127.0.0.1:1",
        "https_proxy": "http://127.0.0.1:1",
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "",
        "no_proxy": "",
        "BOTLIFE_STATE_DB": str(tmp_path / "state.db"),
    })
    if api_key:
        env["DEEPSEEK_API_KEY"] = api_key
    return env


def _run(env, cwd):
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)


def _rows(db_path, bot_id):
    """daily_wildcards 里某只 bot 的全部记录；行内所有列原样取出（不依赖列名清单）。"""
    try:
        con = sqlite3.connect(str(db_path))
        try:
            return con.execute(
                "SELECT rowid, * FROM daily_wildcards WHERE bot_id = ? ORDER BY rowid",
                (bot_id,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        pytest.fail("查询 daily_wildcards 失败（库或表不可用）：%s" % exc)


def _today_forms(day):
    return {
        day.isoformat(),
        "%04d/%02d/%02d" % (day.year, day.month, day.day),
        "%04d年%02d月%02d日" % (day.year, day.month, day.day),
        "%04d年%d月%d日" % (day.year, day.month, day.day),
    }


# ---------------------------------------------------------------- 行为清单 1

def test_行为1_启用bot当天无卡_写入5到8张且全部used为0(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"chenlulu")
    r = _run(_make_env(tmp_path, fake_llm), tmp_path)
    assert r.returncode == 0, "退出码应为 0，实际 %d\nstdout:\n%s\nstderr:\n%s" % (
        r.returncode, r.stdout, r.stderr)
    rows = _rows(tmp_path / "state.db", "chenlulu")
    assert 5 <= len(rows) <= 8, "应为 5-8 张卡，实际 %d 张：%r" % (len(rows), rows)
    used = {row[2] for row in rows}
    assert used == {0}, "所有卡 used 应为 0，实际出现 %r" % (used,)


# ---------------------------------------------------------------- 行为清单 2

def test_行为2_当天已有卡_一条不删不增也不改(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"chenlulu")
    env = _make_env(tmp_path, fake_llm)
    r1 = _run(env, tmp_path)
    assert r1.returncode == 0, "首次运行应成功：%s" % r1.stderr

    db = tmp_path / "state.db"
    seeded = _rows(db, "chenlulu")
    assert 5 <= len(seeded) <= 8, "首次运行应写入 5-8 张卡，实际 %d" % len(seeded)

    # 人为把当天卡改成 2 张、且都标成已使用，验证第二次运行既不补也不覆盖
    keep = [row[0] for row in seeded[:2]]
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "DELETE FROM daily_wildcards WHERE bot_id = ? AND rowid NOT IN (%s)"
            % ",".join("?" * len(keep)), ["chenlulu"] + keep)
        con.execute("UPDATE daily_wildcards SET used = 1 WHERE bot_id = ?", ("chenlulu",))
        con.commit()
    finally:
        con.close()
    before = _rows(db, "chenlulu")
    assert len(before) == 2 and {row[2] for row in before} == {1}

    r2 = _run(env, tmp_path)
    assert r2.returncode == 0, "第二次运行退出码应为 0：%s" % r2.stderr
    assert _rows(db, "chenlulu") == before, "当天已有卡时原有记录应逐列保持不变"


# ---------------------------------------------------------------- 行为清单 3

def test_行为3_停用bot不写卡_启用bot照常写(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"sleeping", enabled=False)
    _require_script()
    _write_bot(tmp_path,"awake", enabled=True)
    r = _run(_make_env(tmp_path, fake_llm), tmp_path)
    assert r.returncode == 0, r.stderr
    assert _rows(tmp_path / "state.db", "sleeping") == [], "停用 bot 名下不应有任何卡"
    assert 5 <= len(_rows(tmp_path / "state.db", "awake")) <= 8, "启用 bot 应照常写入 5-8 张"


# ---------------------------------------------------------------- 行为清单 4

def test_行为4_一只LLM失败_失败者无卡另一只正常且退出0(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"alpha")
    _require_script()
    _write_bot(tmp_path,"beta")
    r = _run(_make_env(tmp_path, fake_llm, mode="fail_first"), tmp_path)
    assert r.returncode == 0, "单只失败时整体退出码仍应为 0，实际 %d：%s" % (
        r.returncode, r.stderr)

    counts = [len(_rows(tmp_path / "state.db", bot)) for bot in ("alpha", "beta")]
    good = [n for n in counts if 5 <= n <= 8]
    bad = [n for n in counts if n == 0]
    assert len(good) == 1 and len(bad) == 1, \
        "应恰好一只成功（5-8 张）、一只失败（0 张），实际 alpha/beta 张数 = %r" % (counts,)
    assert (r.stdout + r.stderr).strip(), "单只失败时应至少记一行日志"


# ---------------------------------------------------------------- 行为清单 5

def test_行为5a_LLM返回空数组_不写库(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"chenlulu")
    r = _run(_make_env(tmp_path, fake_llm, mode="empty"), tmp_path)
    assert r.returncode == 0, "空数组属于单只跳过，退出码应为 0：%s" % r.stderr
    assert _rows(tmp_path / "state.db", "chenlulu") == [], "LLM 返回 [] 时不应写库"


def test_行为5b_LLM返回非数组_不写库(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"chenlulu")
    r = _run(_make_env(tmp_path, fake_llm, mode="dict"), tmp_path)
    assert r.returncode == 0, "非数组属于单只跳过，退出码应为 0：%s" % r.stderr
    assert _rows(tmp_path / "state.db", "chenlulu") == [], "LLM 返回非数组时不应写库"


# ---------------------------------------------------------------- 行为清单 6

def test_行为6_同一天跑两次_第二次不新增卡(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"chenlulu")
    env = _make_env(tmp_path, fake_llm)
    db = tmp_path / "state.db"

    r1 = _run(env, tmp_path)
    assert r1.returncode == 0, "第一次运行应成功：%s" % r1.stderr
    first = _rows(db, "chenlulu")
    assert 5 <= len(first) <= 8, "第一次应写入 5-8 张，实际 %d" % len(first)

    r2 = _run(env, tmp_path)
    assert r2.returncode == 0, "第二次运行应成功：%s" % r2.stderr
    second = _rows(db, "chenlulu")
    assert len(second) == len(first), "第二次运行不应新增卡：%d -> %d" % (
        len(first), len(second))
    assert second == first, "第二次运行不应改动第一次写入的记录"


# ---------------------------------------------------------------- 行为清单 7

def test_行为7_提示词要求第一人称不以你相称_并带当天日期(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"chenlulu")
    env = _make_env(tmp_path, fake_llm)
    dump = Path(env["FAKE_DW_DUMP"])

    r = _run(env, tmp_path)
    assert r.returncode == 0, r.stderr
    assert dump.is_file() and dump.stat().st_size > 0, "脚本没有调用 LLM，拿不到提示词"

    prompt_text = dump.read_text(encoding="utf-8")
    assert "第一人称" in prompt_text, "提示词应包含“第一人称”要求"
    assert "不要出现你字" in prompt_text, "提示词应包含“不要出现你字”要求"
    today = date.today()
    forms = _today_forms(today)
    assert any(form in prompt_text for form in forms), \
        "提示词应带上当天日期（本地日期 %s），实际未找到；提示词记录：%s" % (
            today.isoformat(), prompt_text[:2000])


# ---------------------------------------------------------------- 错误契约

def test_错误契约_DeepSeek密钥缺失_跳过该只且退出0(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"chenlulu")
    r = _run(_make_env(tmp_path, fake_llm, mode="fail_key", api_key=None), tmp_path)
    assert r.returncode == 0, "密钥缺失时整体退出码应为 0，实际 %d：%s" % (
        r.returncode, r.stderr)
    assert _rows(tmp_path / "state.db", "chenlulu") == [], "密钥缺失时不应写卡"


def test_错误契约_API报错_跳过该只且退出0(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"chenlulu")
    r = _run(_make_env(tmp_path, fake_llm, mode="fail_all"), tmp_path)
    assert r.returncode == 0, "API 报错时整体退出码应为 0，实际 %d：%s" % (
        r.returncode, r.stderr)
    assert _rows(tmp_path / "state.db", "chenlulu") == [], "API 报错时不应写卡"


def test_错误契约_BOTLIFE_STATE_DB未设_拒绝运行且非0退出(tmp_path, fake_llm):
    _require_script()
    _write_bot(tmp_path,"chenlulu")
    env = _make_env(tmp_path, fake_llm)
    env.pop("BOTLIFE_STATE_DB", None)
    r = _run(env, tmp_path)
    assert r.returncode != 0, "BOTLIFE_STATE_DB 未设时应拒绝运行并非 0 退出，实际退出码 0"
    assert not (tmp_path / "state.db").exists(), "拒绝运行时不应在工作目录落库"
