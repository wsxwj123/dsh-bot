# -*- coding: utf-8 -*-
"""「这只 bot 启用没有」只有一个读法的白盒单测（INTERFACE-新系统bot运维 §0 第 8 条、
§5b 第 4 条末尾，第 7 稿）。

读法：新系统配置根下 `<bot_id>.yml` **自己**顶层写的 `enabled`，按文件原文判，没写算启用。
它的 `life_config` 指向的那份文件里写的 `enabled` 不参与。三处取的是同一个函数
`bot_launchd.enabled_of` 的结果：列表 `disabled` 里新系统行的标注、重启全部与开关同步的
方向、单只重启的 `bot_disabled`。

此前 `enabled_of` 读的是合并过 `life_config` 的配置，列表的标注走的却是另一个按文件原文
判的函数，所以会出现徽标说启用、重启全部却把它停掉的局面。

只读配置、不起进程（列表那两条用单测默认的只答 print 的假 launchctl）；bot 名全是假名。
"""
import pytest

from moments import bot_launchd as bl
from moments import bots_client as bc
from moments import config_sources as cs


@pytest.fixture
def roots(tmp_path, monkeypatch):
    legacy, dsh, life, home = (tmp_path / n for n in ("legacy", "dsh", "life", "home"))
    for d in (legacy, dsh, life, home):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))   # Windows 上 expanduser 读它
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    return legacy, dsh, life


def _line(value):
    """`enabled` 那一行的原文；None = 不写这个键。布尔写成 true/false，字符串原样照抄。"""
    if value is None:
        return []
    return ["enabled: %s" % (("true" if value else "false") if isinstance(value, bool) else value)]


def _bot(roots, bot_id, own=None, life="no-life", port=17951):
    """造一只新系统的 bot。`own` 是它自己写的 enabled；`life` 给了就带一份 life_config，
    值是那份文件里写的 enabled（`"no-life"` = 不带 life_config）。"""
    _legacy, dsh, life_dir = roots
    lines = ["id: %s" % bot_id, "dispatcher_port: %d" % port] + _line(own)
    if life != "no-life":
        life_yml = life_dir / ("%s.yml" % bot_id)
        life_yml.write_text("\n".join(["display_name: life里的名字"] + _line(life)) + "\n",
                            encoding="utf-8")
        lines.append("life_config: %s" % life_yml)
    yml = dsh / ("%s.yml" % bot_id)
    yml.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return yml


# ------------------------------------------------------------ 读法本身

@pytest.mark.parametrize("own,life,expected", [
    (None, False, True),            # 接口约定的正例：自己不写，life 那份写停用 → 算启用
    (False, True, False),           # 接口约定的反例：自己写停用，life 那份写启用 → 算停用
    (True, False, True),
    (None, True, True),
    (None, None, True),
    (False, False, False),
    (False, None, False),
    (None, "no-life", True),        # 不带 life_config 的普通 bot：没写算启用
    (False, "no-life", False),
    ("0", True, False),             # 手写的假值照 bots_registry.is_enabled 的口径算停用
    ("no", "no-life", False),
    ('""', True, False),
    ("yes", False, True),
])
def test_只看新系统那份自己写的enabled_life那份写的不参与(roots, own, life, expected):
    _bot(roots, "xbot", own=own, life=life)
    assert bl.enabled_of("xbot") is expected


def test_合并后的配置确实带着life那份的enabled_本用例的前提(roots):
    """前提自检：全系统共用的读配置会把 life 那份的 enabled 合并进来，所以不能拿它判。"""
    _bot(roots, "xbot", own=None, life=False)
    assert cs.effective_config("xbot").get("enabled") is False
    assert bl.enabled_of("xbot") is True


def test_按文件原文判_读不出来的不算启用_没写的算启用(tmp_path):
    missing = tmp_path / "missing.yml"
    broken = tmp_path / "broken.yml"
    broken.write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    plain = tmp_path / "plain.yml"
    plain.write_text("id: xbot\n", encoding="utf-8")
    off = tmp_path / "off.yml"
    off.write_text("id: xbot\nenabled: false\n", encoding="utf-8")
    assert [cs.is_enabled_file(p) for p in (missing, broken, plain, off)] == [
        False, False, True, False]


def test_新系统那份解析不出来_这只不算启用(roots):
    """坏配置进不了名单，第一步（来源）就回否；轮不到按文件原文判那一步。"""
    _legacy, dsh, _life = roots
    (dsh / "xbot.yml").write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    assert bl.enabled_of("xbot") is False
    (dsh / "ybot.yml").write_text("just a string\n", encoding="utf-8")     # 顶层不是映射
    assert bl.enabled_of("ybot") is False


def test_来源条件还在_生效的是旧系统那份时照旧回否(roots):
    legacy, _dsh, _life = roots
    (legacy / "xbot.yml").write_text("id: xbot\n", encoding="utf-8")
    _bot(roots, "xbot", own=False, life=True)       # 新系统那份停着 → 生效的是旧系统那份
    assert bl.enabled_of("xbot") is False


# ------------------------------------------------------------ 三处取同一个结果

def test_重启全部的方向_逐只按同一个读法(roots):
    _bot(roots, "abot", own=None, life=False, port=17951)
    _bot(roots, "bbot", own=False, life=True, port=17952)
    _bot(roots, "cbot", port=17953)
    assert bl.restart_pairs() == [("abot", True), ("bbot", False), ("cbot", True)]


@pytest.fixture
def client(roots):
    from flask import Flask

    from moments.hub_routes import hub_bp
    app = Flask(__name__)
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_列表的停用标注_新系统行与方向同一个读法(roots, client):
    _bot(roots, "abot", own=None, life=False, port=17951)
    _bot(roots, "bbot", own=False, life=True, port=17952)
    _bot(roots, "cbot", port=17953)
    body = client.get("/hub/api/bots").get_json()
    assert [b["id"] for b in body["bots"]] == ["abot", "bbot", "cbot"]
    assert body["disabled"] == ["bbot"]


def test_列表的停用标注_新系统行取的就是enabled_of的结果(roots, client, monkeypatch):
    """接线：换掉 `enabled_of` 的答案，标注跟着变 —— 说明没有另读一遍。"""
    _bot(roots, "abot", port=17951)
    _bot(roots, "bbot", port=17952)
    monkeypatch.setattr(bl, "enabled_of", lambda bot_id: bot_id != "abot")
    assert client.get("/hub/api/bots").get_json()["disabled"] == ["abot"]


def test_列表的停用标注_兜底显示出来的其余行保持既有口径(roots, client, monkeypatch):
    """新系统根被关掉时页面兜底列出旧系统的行；它们的标注不走 `enabled_of`
    （它对旧系统的 bot 一律回否，拿来标注会把启用的也标成停用）。"""
    legacy, _dsh, _life = roots
    (legacy / "oldon.yml").write_text("id: oldon\ndispatcher_port: 17801\n", encoding="utf-8")
    (legacy / "oldoff.yml").write_text("id: oldoff\nenabled: false\ndispatcher_port: 17802\n",
                                       encoding="utf-8")
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    monkeypatch.setattr(bc, "_probe_one", lambda port: None)        # 不真发本机探活
    body = client.get("/hub/api/bots").get_json()
    assert [b["id"] for b in body["bots"]] == ["oldoff", "oldon"]
    assert body["disabled"] == ["oldoff"]


def test_单只重启的停用判定_与方向同一个读法(roots, client, monkeypatch):
    """设了覆盖命令：可用性恒真，受理之后只跑那一条什么都不做的命令，不碰 launchctl。
    `bot_disabled` 这一格照常判，正好单独量它。"""
    import sys
    import time
    monkeypatch.setenv("HUB_RESTART_CMD", '"%s" -c "pass"' % sys.executable)
    _bot(roots, "abot", own=None, life=False, port=17951)
    _bot(roots, "bbot", own=False, life=True, port=17952)
    r = client.post("/hub/api/bots/bbot/restart")       # 自己写着停用：拒
    assert (r.status_code, r.get_json()["error"]) == (409, "bot_disabled")
    r = client.post("/hub/api/bots/abot/restart")       # 自己没写、life 那份写停用：不算停用
    assert r.status_code == 202, r.get_json()
    jobs, job_id = client.application.extensions["hub_restart_jobs"], r.get_json()["job_id"]
    deadline = time.time() + 15
    while jobs.get(job_id)["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert jobs.get(job_id)["state"] == "done", jobs.get(job_id)
