# -*- coding: utf-8 -*-
"""适配服务的供应商清单存档（provider-catalog-snapshot）：形状核对、重写节奏、写不成的退路。

直接调 ``_load_catalog``、``_save_catalog``，存档落在本用例的临时 ``DSH_BOT_HOME`` 下。
走 HTTP 的几条（在跑时存、全停时回 cached_at、认不出不顶替）在验收测试里，这里不重复。
"""
import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from moments import adapter_service as svc

FMT = "%Y-%m-%dT%H:%M:%SZ"
CATALOG = [{"id": "va", "name": "va", "has_key": True, "models": ["a1", "a2"]},
           {"id": "vb", "name": "vb", "has_key": False, "models": []}]


@pytest.fixture
def archive(monkeypatch, tmp_path):
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path / "dsh-home"))
    return tmp_path / "dsh-home" / "state" / "provider-catalog.json"


def _stamp(delta=timedelta(0)):
    return (datetime.now(timezone.utc) + delta).strftime(FMT)


def _put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


def test_没有存档_读回None(archive):
    assert svc._load_catalog() is None


def test_存下再读回_清单一字不差_state目录自动建_0600_只有两个键(archive):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    svc._save_catalog(CATALOG)
    assert stat.S_IMODE(os.stat(archive).st_mode) == 0o600
    data = json.loads(archive.read_text(encoding="utf-8"))
    assert set(data) == {"saved_at", "providers"} and data["providers"] == CATALOG
    providers, saved_at = svc._load_catalog()
    assert providers == CATALOG and saved_at == data["saved_at"]
    assert before <= svc._catalog_time(saved_at) <= datetime.now(timezone.utc)


def test_空清单也是合规的存档(archive):
    _put(archive, {"saved_at": _stamp(), "providers": []})
    assert svc._load_catalog() == ([], json.loads(archive.read_text())["saved_at"])


def test_名字里有落单的代理码位也写得出读得回(archive):
    odd = [{"id": "v\ud800", "name": "v\ud800", "has_key": True, "models": ["m\udfff", "中文"]}]
    svc._save_catalog(odd)
    assert svc._load_catalog()[0] == odd


def _item(**over):
    item = {"id": "va", "name": "va", "has_key": True, "models": ["a1"]}
    item.update(over)
    return item


BAD = {
    "顶层是数组": [_item()],
    "顶层多一个键": {"saved_at": "NOW", "providers": [_item()], "note": "x"},
    "缺saved_at": {"providers": [_item()]},
    "saved_at不是字符串": {"saved_at": 1760000000, "providers": [_item()]},
    "saved_at带时区偏移": {"saved_at": "2026-10-17T02:15:00+08:00", "providers": [_item()]},
    "saved_at没补零": {"saved_at": "2026-1-7T2:15:00Z", "providers": [_item()]},
    "saved_at带毫秒": {"saved_at": "2026-10-17T02:15:00.000Z", "providers": [_item()]},
    "saved_at是不存在的日子": {"saved_at": "2026-02-30T00:00:00Z", "providers": [_item()]},
    "providers是对象": {"saved_at": "NOW", "providers": {"va": _item()}},
    "某项不是对象": {"saved_at": "NOW", "providers": [_item(), "vb"]},
    "某项多带keyEnv": {"saved_at": "NOW", "providers": [_item(keyEnv="VA_API_KEY")]},
    "某项缺has_key": {"saved_at": "NOW", "providers": [{"id": "va", "name": "va", "models": []}]},
    "id是空串": {"saved_at": "NOW", "providers": [_item(id="")]},
    "name全是空白": {"saved_at": "NOW", "providers": [_item(name="  ")]},
    "name不是字符串": {"saved_at": "NOW", "providers": [_item(name=7)]},
    "has_key是字符串": {"saved_at": "NOW", "providers": [_item(has_key="true")]},
    "has_key是1": {"saved_at": "NOW", "providers": [_item(has_key=1)]},
    "models不是数组": {"saved_at": "NOW", "providers": [_item(models="a1")]},
    "models里有非字符串": {"saved_at": "NOW", "providers": [_item(models=["a1", None])]},
    "后一项坏_前一项好也整份不用": {"saved_at": "NOW", "providers": [_item(), _item(id="vb", models=None)]},
}


@pytest.mark.parametrize("content", list(BAD.values()), ids=list(BAD))
def test_形状不对_整份不用(archive, content):
    if isinstance(content, dict) and content.get("saved_at") == "NOW":
        content = dict(content, saved_at=_stamp())
    _put(archive, content)
    assert svc._load_catalog() is None


@pytest.mark.parametrize("raw", [b"not json {{{", b"", b"\xff\xfe", b"[" * 100000],
                         ids=["不是JSON", "空文件", "非UTF8", "嵌套过深"])
def test_读不出_回None(archive, raw):
    archive.parent.mkdir(parents=True)
    archive.write_bytes(raw)
    assert svc._load_catalog() is None


def test_清单没变且不到10分钟_不重写(archive):
    _put(archive, {"saved_at": _stamp(timedelta(minutes=-5)), "providers": CATALOG})
    before = archive.read_bytes()
    svc._save_catalog(CATALOG)
    assert archive.read_bytes() == before


@pytest.mark.parametrize("providers, delta", [
    (CATALOG, timedelta(minutes=-11)),          # 没变，但上次存的超过 10 分钟
    (CATALOG[:1], timedelta(minutes=-1)),       # 变了（少一家）
    (list(reversed(CATALOG)), timedelta(0)),    # 变了（先后不同）
    (CATALOG, timedelta(hours=1)),              # 存档的时间在将来：不当作"刚存过"
], ids=["超过10分钟", "少一家", "先后变了", "时间在将来"])
def test_该重写时重写_时间换成此刻_权限回到0600(archive, providers, delta):
    _put(archive, {"saved_at": _stamp(delta), "providers": providers})
    archive.chmod(0o644)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    svc._save_catalog(CATALOG)
    providers_now, saved_at = svc._load_catalog()
    assert providers_now == CATALOG
    assert before <= svc._catalog_time(saved_at) <= datetime.now(timezone.utc)
    assert stat.S_IMODE(os.stat(archive).st_mode) == 0o600


def test_存档坏了_有新清单时直接覆盖(archive):
    _put(archive, "garbage")
    svc._save_catalog(CATALOG)
    assert svc._load_catalog()[0] == CATALOG


def test_写不成_不抛_日志只记异常类名不写清单(archive, caplog):
    archive.parent.parent.mkdir(parents=True)
    archive.parent.write_text("state 被占成了文件", encoding="utf-8")     # makedirs 必然失败
    with caplog.at_level(logging.ERROR, logger="dsh.adapter"):
        svc._save_catalog(CATALOG)
    messages = [r.getMessage() for r in caplog.records if r.name == "dsh.adapter"]
    assert messages == ["provider catalog snapshot failed: FileExistsError"]
