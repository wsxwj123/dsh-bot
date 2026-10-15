# -*- coding: utf-8 -*-
"""鉴权门在口令读不到时不放行：启动时配过口令、或 hub.env 在却读不出来，一律 503，不按「没设口令」裸奔。"""
import os
import sys
from pathlib import Path

import pytest
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from moments import hub_auth as A  # noqa: E402

PW = "hub-access-pw-12345"


def _app(monkeypatch, tmp_path, access=None, env_bytes=None, env_mode=None):
    env = tmp_path / "hub.env"
    monkeypatch.setenv("HUB_ENV_FILE", str(env))
    monkeypatch.setenv("MOMENTS_WEB_HOST", "127.0.0.1")
    monkeypatch.delenv("HUB_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("HUB_ACCESS_PASSWORD", access) if access else monkeypatch.delenv("HUB_ACCESS_PASSWORD", raising=False)
    if env_bytes is not None:
        env.write_bytes(env_bytes)
        if env_mode is not None:
            os.chmod(env, env_mode)
    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.route("/")
    def home():
        return "feed"

    A.install(app)
    A.remember_configured(app)        # 同 web.py 的 __main__：真的开门户时记一次
    return app.test_client()


def test_启动时配过口令_之后读不到_拒绝而不是放行(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path, access=PW)
    monkeypatch.delenv("HUB_ACCESS_PASSWORD")
    r = c.get("/")
    assert r.status_code == 503 and r.get_json()["error"] == "auth_unavailable"
    assert c.get("/hub/healthz").status_code != 503


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="靠权限位读不出")
def test_hub_env在却没权限读_拒绝(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path, env_bytes=b"HUB_ACCESS_PASSWORD=x\n", env_mode=0)
    try:
        assert c.get("/").status_code == 503
    finally:
        os.chmod(tmp_path / "hub.env", 0o600)


def test_hub_env不是UTF8_拒绝(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path, env_bytes=b"HUB_ACCESS_PASSWORD=\xff\xfe\n")
    assert c.get("/").status_code == 503


def test_从没配过口令也没有hub_env_照旧放行(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path)
    r = c.get("/")
    assert r.status_code == 200 and r.data == b"feed"
