# -*- coding: utf-8 -*-
"""真网关端到端：真 Flask 管理台 -> 真网关进程 -> 真落盘文件。

假件只用在最外层：假 Telegram、假模型列表接口。中间的管理台与网关都是真进程（Flask 用 test_client，
网关用 bun 子进程）。没有 bun 时整批 skip。
"""
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from .conftest import BUN
from .stub_gateway import StubGateway

pytestmark = pytest.mark.skipif(not BUN, reason="需要 bun 才能起真网关")

SAVE = "/hub/api/dsh-model/provider/save"
REMOVE = "/hub/api/dsh-model/provider/{}/remove"
KEY = "test-key-7e2e-unique-0000000001"


@pytest.fixture
def models_stub():
    """假模型列表接口（对方那一层）：OpenAI 形，路径以 /models 结尾。"""
    s = StubGateway(key=KEY).start()
    s.on("/v1/models", 200, {"object": "list", "data": [
        {"id": "m1", "object": "model"}, {"id": "m2", "object": "model"}]})
    try:
        yield s
    finally:
        s.stop()


def gw_call(rg, method, path, body=None):
    """带口令调真网关的本机接口。"""
    key = rg["key_path"].read_text(encoding="utf-8").strip()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (rg["port"], path), data=data, method=method,
                                 headers={"content-type": "application/json", "authorization": "Bearer %s" % key})
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def save_payload(models_stub):
    return {"name": "myproxy", "api": "openai-completions",
            "baseURL": "http://127.0.0.1:%d/v1" % models_stub.port, "key": KEY, "mode": "create"}


def seed_provider(root, name, base_url, models=("s1", "s2")):
    """直接按契约 4 的落盘格式写 providers.json（前置条件用，网关每次现读）。"""
    p = Path(root) / "providers.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else \
        {"version": 1, "providers": {}, "pendingKeyRemovals": []}
    data["providers"][name] = {
        "route": {"api": "openai-completions", "baseURL": base_url,
                  "apiKeyEnv": "PROVIDER_%s_KEY" % name.upper().replace("-", "_"),
                  "models": [{"id": m, "contextWindow": 131072} for m in models]},
        "meta": {"createdAt": 1700000000000, "updatedAt": 1700000000000, "epoch": 1700000000000,
                 "keyRev": 1, "guessedContext": list(models), "ownerContext": [],
                 "manualModels": [], "lastRefresh": None},
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def test_真网关_管理台添加后网关侧与落盘都对(hub, real_gateway, models_stub):
    r = hub.post(SAVE, json=save_payload(models_stub))
    assert r.status_code == 200, "管理台添加应成功，实得 %s（%s）" % (r.status_code, r.get_data(as_text=True)[:200])
    assert r.get_json().get("count") == 2, "拉到 2 个模型，实得 %s" % r.get_json()
    code, view = gw_call(real_gateway, "GET", "/v1/provider")
    assert code == 200, "真网关的 /v1/provider 应 200，实得 %s" % code
    names = [p.get("name") for p in view.get("providers") or []]
    assert "myproxy" in names, "网关侧应看到管理台添加的供应商，实得 %s" % names
    root = real_gateway["root"]
    providers = json.loads((root / "providers.json").read_text(encoding="utf-8"))
    assert "myproxy" in providers["providers"], "providers.json 里应有它"
    creds = (root / "credentials.yaml").read_text(encoding="utf-8")
    assert KEY in creds, "凭据文件里应有这把密钥"


def test_真网关_管理台列表能看到网关里已有的供应商(hub, real_gateway):
    """契约 2.1：管理台列表读到的是网关现读的那份 providers.json。"""
    seed_provider(real_gateway["root"], "seeded", "https://seeded.example.com/v1", models=("s1", "s2"))
    j = hub.get("/hub/api/dsh-model").get_json()
    names = [p.get("name") for p in (j.get("providers") or [])]
    assert "seeded" in names, "管理台列表应列出网关里已有的供应商，实得 %s" % names
    proj = [p.get("name") for p in (j.get("custom_providers") or [])]
    assert "seeded" in proj, "custom_providers 也应含它，实得 %s" % proj


def test_真网关_管理台删除后网关侧也没了(hub, real_gateway, models_stub):
    assert hub.post(SAVE, json=save_payload(models_stub)).status_code == 200, "前置条件 添加应成功"
    r = hub.post(REMOVE.format("myproxy"), json={})
    assert r.status_code == 200, "删除应成功，实得 %s（%s）" % (r.status_code, r.get_data(as_text=True)[:200])
    code, view = gw_call(real_gateway, "GET", "/v1/provider")
    names = [p.get("name") for p in view.get("providers") or []]
    assert "myproxy" not in names, "网关侧应看不到被删的供应商，实得 %s" % names


def test_真网关_密钥只在credentials_yaml里(hub, real_gateway, models_stub, capsys):
    import time
    capsys.readouterr()
    r = hub.post(SAVE, json=save_payload(models_stub))
    assert r.status_code == 200, "前置条件 添加应成功，实得 %s" % r.status_code
    root = real_gateway["root"]
    time.sleep(1.0)                    # 给日志与文件写入一点落定时间
    skip = (root / "credentials.yaml").resolve()
    hits = []
    for p in Path(root).rglob("*"):
        if p.is_dir() or p.resolve() == skip:
            continue
        try:
            if KEY.encode("utf-8") in p.read_bytes():
                hits.append(str(p.relative_to(root)))
        except OSError:
            pass
    assert hits == [], "密钥出现在不该出现的地方（除 credentials.yaml 外应为空）：%s" % hits
    gw_out = real_gateway["stdout_path"].read_text(encoding="utf-8", errors="replace")
    assert KEY not in gw_out, "密钥出现在了网关的标准输出里"
    out = capsys.readouterr()
    assert KEY not in (out.out + out.err), "密钥出现在了管理台的输出里"
    assert KEY not in r.get_data(as_text=True), "密钥出现在了管理台的响应里"
    assert KEY in (root / "credentials.yaml").read_text(encoding="utf-8"), "凭据文件里应恰恰有它"
