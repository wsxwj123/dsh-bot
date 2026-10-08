# -*- coding: utf-8 -*-
"""F6：管理台新系统模型页的自建供应商小表与「刷新模型列表」（INTERFACE 3.9）。

隔离环境里没有任何在跑的新系统网关；需要"连上网关"的分支（透传网关状态码、504 超时、custom_providers 非空）
这里测不到，原因见 TEST-PLAN.md 的"没覆盖"。
"""
from urllib.parse import quote

import pytest

REFRESH = "/hub/api/dsh-model/provider/{}/refresh"


def _json(r):
    try:
        return r.get_json()
    except Exception:
        return None


def test_GET_dsh_model_原有bots键仍在(hub):
    r = hub.get("/hub/api/dsh-model")
    assert r.status_code == 200
    assert isinstance(_json(r).get("bots"), list)


def test_GET_dsh_model_新增custom_providers_没有网关时为空列表(hub):
    r = hub.get("/hub/api/dsh-model")
    assert r.status_code == 200
    assert _json(r).get("custom_providers") == []


def test_刷新_请求体不是JSON_400_bad_body(hub):
    r = hub.post(REFRESH.format("myproxy"), data="not json", content_type="text/plain")
    assert r.status_code == 400
    assert (_json(r) or {}).get("error") == "bad_body"


def test_刷新_请求体是JSON数组不是对象_400_bad_body(hub):
    r = hub.post(REFRESH.format("myproxy"), data="[1, 2]", content_type="application/json")
    assert r.status_code == 400
    assert (_json(r) or {}).get("error") == "bad_body"


@pytest.mark.parametrize("name", ["bad.name", "-abc", "_abc", "a" * 33, "中文名", "a b"])
def test_刷新_名字不合规则_400_bad_provider(hub, name):
    r = hub.post(REFRESH.format(quote(name, safe="")), json={})
    assert r.status_code == 400
    assert (_json(r) or {}).get("error") == "bad_provider"


def test_刷新_没有在跑的新系统网关_503_no_gateway及确切文案(hub):
    r = hub.post(REFRESH.format("myproxy"), json={})
    assert r.status_code == 503
    assert _json(r) == {"ok": False, "error": "no_gateway", "text": "没有在运行的新系统 bot"}


def test_刷新_名字含大写和下划线合规_走到找网关这一步(hub):
    r = hub.post(REFRESH.format("My_Proxy"), json={})
    assert r.status_code == 503
    assert (_json(r) or {}).get("error") == "no_gateway"


def test_刷新_32个字符的名字合规(hub):
    r = hub.post(REFRESH.format("a" * 31 + "1"), json={})
    assert (_json(r) or {}).get("error") == "no_gateway"


def test_页面_有自建供应商小表与刷新模型列表按钮(hub):
    r = hub.get("/hub/dsh-model")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "自建供应商" in html
    assert "刷新模型列表" in html
