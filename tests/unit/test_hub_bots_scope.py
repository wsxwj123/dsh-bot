# -*- coding: utf-8 -*-
"""`/hub/bots` 那一页只管新系统的 bot。

判「这个 bot 属于哪套」不另写一套：`config_sources` 的"生效那份"口径说了算
（在跑的那份优先，没有在跑的按覆盖规则取优先级最高那份），`bots_client.hub_visible`
只按那个来源标记筛。旧系统在跑的、只在旧根里的一律不进这一页，它们由旧面板与
桌面脚本管；在新系统里停用的仍留在表里（灰一档），那一页是唯一能把它点回来的
地方。「重启全部 bot」同理，只覆盖新系统：命令的配置根由管理台限定。

不读真实 `~/.dsh-bot`：HOME 钉在 tmp，默认的新系统根路径自然落空。
"""
import time
from pathlib import Path

import pytest
from flask import Flask

from moments import bots_client
from moments.hub_routes import hub_bp

TEMPLATES = Path(__file__).resolve().parents[2] / "moments" / "templates"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """两套 tmp 配置根（都在）+ 钉住的 HOME；每个用例从"没有接缝值"开始。"""
    for k in ("HUB_BOTS_FILE", "HUB_RESTART_CMD"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    legacy = tmp_path / "legacy"
    dsh = tmp_path / "dsh"
    for d in (home, legacy, dsh):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    return legacy, dsh


@pytest.fixture
def client(monkeypatch):
    """不真探活（配置里不写端口时探活会落到派生端口，可能打到生产 dispatcher）。"""
    monkeypatch.setattr(bots_client, "_probe_one", lambda port: None)
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _write(root, name, **fields):
    lines = ["id: %s" % name]
    for k, v in sorted(fields.items()):
        if isinstance(v, bool):
            v = "true" if v else "false"
        lines.append("%s: %s" % (k, v))
    (Path(root) / ("%s.yml" % name)).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _wait_done(jobs, job_id, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        st = jobs.get(job_id)
        if st and st["state"] != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("重启任务 %s 没在 %s 秒内跑完" % (job_id, timeout))


# ------------------------------------------------------------ 名单范围

def test_双根_旧系统的那只不进这一页(roots, client):
    """bot2 旧根在跑、新根停用：四个出口（名单、来源、停用徽标、覆盖点名）都不出现。"""
    legacy, dsh = roots
    _write(legacy, "bot2", display_name="李彤彤", dispatcher_port=17899)
    _write(dsh, "bot2", enabled=False, display_name="李彤彤", dispatcher_port=17952)
    _write(dsh, "bot4", display_name="陈露露", dispatcher_port=17951)
    body = client.get("/hub/api/bots").get_json()
    assert [b["id"] for b in body["bots"]] == ["bot4"]
    assert set(body["sources"]) == {"bot4"}
    assert body["disabled"] == [] and body["overrides"] == []


def test_新系统里停用的bot仍留在表里(roots, client):
    """从列表里消失 = 停了以后再也点不回来。它在 disabled 里，页面据此灰一档。"""
    _legacy, dsh = roots
    _write(dsh, "bot4", display_name="陈露露", dispatcher_port=17951)
    _write(dsh, "bot5", enabled=False, display_name="bot5", dispatcher_port=17950)
    body = client.get("/hub/api/bots").get_json()
    assert [b["id"] for b in body["bots"]] == ["bot4", "bot5"]
    assert body["disabled"] == ["bot5"]
    assert body["sources"]["bot5"]["source"] == "dsh"


def test_两边同名都停用_按新系统那份留在表里(roots, client):
    """两份都停着时按覆盖规则取新系统那份，它仍属于这一页（有配置、点得回来）。"""
    legacy, dsh = roots
    _write(legacy, "bot5", enabled=False, display_name="旧五", dispatcher_port=17899)
    _write(dsh, "bot5", enabled=False, display_name="新五", dispatcher_port=17950)
    body = client.get("/hub/api/bots").get_json()
    assert [b["id"] for b in body["bots"]] == ["bot5"]
    assert body["disabled"] == ["bot5"]


def test_新系统那套根不在场时不过滤(roots, client, monkeypatch):
    """单套根的机器没有新旧之分，那套根就是它自己的根，照旧全显示。"""
    legacy, _dsh = roots
    _write(legacy, "bot2", display_name="李彤彤", dispatcher_port=17899)
    _write(legacy, "bot3", enabled=False, display_name="菜菜", dispatcher_port=17898)
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    body = client.get("/hub/api/bots").get_json()
    assert [b["id"] for b in body["bots"]] == ["bot2", "bot3"]
    assert body["disabled"] == ["bot3"]


def test_注入名单时不猜来源_原样放行(roots, client, monkeypatch, tmp_path):
    """HUB_BOTS_FILE 是测试接缝，与配置目录无关；来源为空不许把名单清空。"""
    p = tmp_path / "bots.json"
    p.write_text('[{"id": "ghost", "display_name": "幽灵"}]', encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    body = client.get("/hub/api/bots").get_json()
    assert [b["id"] for b in body["bots"]] == ["ghost"]


def test_来源读不出来时名单照旧(roots, client, monkeypatch):
    """fail-open：来源标注缺失不该把名单清空（宁可多显示）。"""
    legacy, dsh = roots
    _write(legacy, "bot2", dispatcher_port=17899)
    monkeypatch.setattr(bots_client, "bot_sources", lambda: {})
    body = client.get("/hub/api/bots").get_json()
    assert [b["id"] for b in body["bots"]] == ["bot2"]


# ------------------------------------------------------------ 重启的范围

def test_重启命令的子进程只拿到新系统的配置根(roots, client, monkeypatch, tmp_path):
    """子进程要按新系统的根读注册表，否则重启脚本会去动旧系统的 bot。"""
    legacy, dsh = roots
    marker = tmp_path / "child-env.txt"
    monkeypatch.setenv("HUB_RESTART_CMD",
                       "sh -c 'echo ${HUB_CONFIGS_DIR:-none} > %s'" % marker)
    r = client.post("/hub/api/bots/restart")
    assert r.status_code == 202, r.get_json()
    jobs = client.application.extensions["hub_restart_jobs"]
    st = _wait_done(jobs, r.get_json()["job_id"])
    assert st["state"] == "done", st
    assert marker.read_text(encoding="utf-8").strip() == str(dsh)


def test_新系统根不在场_重启命令不动用户环境(roots, client, monkeypatch, tmp_path):
    """单套根的机器照原样继承环境，别去改用户自己那份配置根。"""
    legacy, _dsh = roots
    marker = tmp_path / "child-env.txt"
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    monkeypatch.setenv("HUB_RESTART_CMD",
                       "sh -c 'echo ${HUB_CONFIGS_DIR:-none} > %s'" % marker)
    r = client.post("/hub/api/bots/restart")
    assert r.status_code == 202, r.get_json()
    jobs = client.application.extensions["hub_restart_jobs"]
    st = _wait_done(jobs, r.get_json()["job_id"])
    assert st["state"] == "done", st
    assert marker.read_text(encoding="utf-8").strip() == str(legacy)


# ------------------------------------------------------------ 页面文案

def test_页面文案说清只管新系统的bot(client):
    html = client.get("/hub/bots").get_data(as_text=True)
    assert "只管新系统的 bot" in html, "页面没说清这一页的范围"
    assert "旧管理台" in html and "桌面脚本" in html, \
        "页面没告诉用户旧系统的 bot 去哪里管"
    assert "重启" in html, "重启入口（验收锁定的那条）还在"
