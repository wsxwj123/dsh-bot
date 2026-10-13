# -*- coding: utf-8 -*-
"""管理台刷新端点对清单指纹 rev 的处理（INTERFACE-dsh修补 3.3、5.4 末段）。

验收测试覆盖了「带 rev 原样转发」与四种形状不对。这里补白盒的几格：
- 转发给网关的请求体只会有 name、only、rev 三样，`full` 不认也不转发；
- rev 的键在而值是 null 算形状不对（没带才是不核对）；恰好 100 个字符放行；
- 名字、only、rev 三道校验的先后。
"""
import pytest

import gateway_client
from moments import hub_routes

BAD_REV = {"ok": False, "error": "bad_rev", "text": "rev 不合法"}
URL = "/hub/api/dsh-model/provider/myproxy/refresh"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """起一个只挂管理台蓝图的应用；转发一律走到假的那一层，记下交给网关客户端的参数。"""
    from flask import Flask

    monkeypatch.setenv("HOME", str(tmp_path))
    sent = []

    def fake_refresh(channel_dir, name, only=None, rev=None):
        sent.append({"name": name, "only": only, "rev": rev})
        return 200, {"ok": True}

    monkeypatch.setattr(gateway_client, "provider_refresh", fake_refresh)
    monkeypatch.setattr(hub_routes, "_dm_forward",
                        lambda call, **_k: (lambda r: (r[1], r[0]))(call("/fake/channel")))
    app = Flask(__name__)
    app.register_blueprint(hub_routes.hub_bp)
    app.config["TESTING"] = True
    return app.test_client(), sent


def test_带rev就转发_没带就不带_恰好100个字符放行(client):
    c, sent = client
    assert c.post(URL, json={"only": ["a"], "rev": "r" * 100}).status_code == 200
    assert c.post(URL, json={"only": ["a"]}).status_code == 200
    assert c.post(URL, json={"rev": "x"}).status_code == 200
    assert sent == [{"name": "myproxy", "only": ["a"], "rev": "r" * 100},
                    {"name": "myproxy", "only": ["a"], "rev": None},
                    {"name": "myproxy", "only": None, "rev": "x"}]


@pytest.mark.parametrize("rev", [None, 123, "", "r" * 101, ["x"], {"a": 1}, True])
def test_rev的键在而值不合格_400_bad_rev_不转发(client, rev):
    c, sent = client
    r = c.post(URL, json={"only": ["a"], "rev": rev})
    assert (r.status_code, r.get_json()) == (400, BAD_REV)
    assert sent == []


@pytest.mark.parametrize("full", [True, False, "yes", None])
def test_请求体里的full不认也不转发_其余照常(client, full):
    c, sent = client
    assert c.post(URL, json={"full": full}).status_code == 200
    assert c.post(URL, json={"only": ["a"], "rev": "x", "full": full}).status_code == 200
    assert sent == [{"name": "myproxy", "only": None, "rev": None},
                    {"name": "myproxy", "only": ["a"], "rev": "x"}]


def test_校验的先后_only先于rev(client):
    c, sent = client
    r = c.post(URL, json={"only": "not-a-list", "rev": 1})
    assert (r.status_code, r.get_json()["error"]) == (400, "bad_only")
    assert sent == []


def test_网关客户端_请求体只有name_only_rev_给了才带(monkeypatch):
    seen = []

    def fake_call(channel_dir, method, path, body=None, timeout=10):
        seen.append((method, path, body))
        return 200, {"ok": True}

    monkeypatch.setattr(gateway_client, "_call", fake_call)
    gateway_client.provider_refresh("/ch", "myproxy")
    gateway_client.provider_refresh("/ch", "myproxy", only=[])
    gateway_client.provider_refresh("/ch", "myproxy", only=["a"], rev="rev-1")
    assert seen == [("POST", "/v1/provider/refresh", {"name": "myproxy"}),
                    ("POST", "/v1/provider/refresh", {"name": "myproxy", "only": []}),
                    ("POST", "/v1/provider/refresh", {"name": "myproxy", "only": ["a"], "rev": "rev-1"})]
