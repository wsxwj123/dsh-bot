# -*- coding: utf-8 -*-
"""适配服务的供应商清单存档（provider-catalog-snapshot）：形状核对、重写节奏、写不成的退路；
第二轮加固的两条：没用的清单（空数组、每家都没模型）不存，读存档不跟软链、只认普通文件、最多 1 MiB。

直接调 ``_load_catalog``、``_save_catalog``，存档落在本用例的临时 ``DSH_BOT_HOME`` 下。
走 HTTP 的几条（在跑时存、全停时回 cached_at、认不出不顶替）在验收测试里，这里不重复。
"""
import json
import logging
import os
import stat
import threading
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


# ---------------------------------------------------------------- 第二轮：没用的清单不存
#: 很久以前存的、内容也和 CATALOG 不同：照"变了或超过 10 分钟就重写"本该被重写
OLD = {"saved_at": "2026-01-01T00:00:00Z",
       "providers": [{"id": "vo", "name": "vo", "has_key": True, "models": ["o1"]}]}
USELESS = {"空数组": [], "每家都没模型": [dict(CATALOG[0], models=[]), CATALOG[1]]}


@pytest.mark.parametrize("catalog", list(USELESS.values()), ids=list(USELESS))
def test_没用的清单_没有存档时也不建(archive, catalog):
    svc._save_catalog(catalog)
    assert not os.path.lexists(archive)


@pytest.mark.parametrize("catalog", list(USELESS.values()), ids=list(USELESS))
def test_没用的清单_已有存档一个字节不动_saved_at不刷新(archive, catalog):
    _put(archive, OLD)
    before = archive.read_bytes()
    svc._save_catalog(catalog)
    assert archive.read_bytes() == before


def test_有一家有模型_别家没有_照常写(archive):
    _put(archive, OLD)
    svc._save_catalog(CATALOG)                  # va 有模型，vb 没有
    assert svc._load_catalog()[0] == CATALOG


# ---------------------------------------------------------------- 第二轮：读存档不跟软链、只认普通文件、最多 1 MiB
def _load_within(seconds=5):
    """在另一个线程里调 ``_load_catalog``，最多等 ``seconds`` 秒：卡住就判失败，不让整套测试挂住。"""
    result = {}
    worker = threading.Thread(target=lambda: result.update(value=svc._load_catalog()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "_load_catalog 卡住了"
    return result["value"]


def test_存档是软链_哪怕指向合规的清单_读回None(archive, tmp_path):
    target = tmp_path / "elsewhere.json"
    _put(target, {"saved_at": _stamp(), "providers": CATALOG})
    archive.parent.mkdir(parents=True)
    archive.symlink_to(target)
    assert json.loads(archive.read_text(encoding="utf-8"))["providers"] == CATALOG   # 跟过去是合规的
    assert svc._load_catalog() is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="这个平台没有 FIFO")
def test_存档是FIFO_读回None_不卡住(archive):
    archive.parent.mkdir(parents=True)
    os.mkfifo(archive)
    assert _load_within() is None


def test_存档是目录_读回None(archive):
    archive.mkdir(parents=True)
    assert _load_within() is None


@pytest.mark.skipif(not os.path.isdir("/dev/fd"), reason="这个平台数不了本进程打开的 fd")
def test_存档是目录_反复读也不漏fd(archive):
    """存档路径是目录：``life_settings._read`` 关掉打开的 fd 再回读不出，读多少次 fd 数都不涨。"""
    archive.mkdir(parents=True)
    before = len(os.listdir("/dev/fd"))
    for _ in range(20):
        assert svc._load_catalog() is None
    assert len(os.listdir("/dev/fd")) == before


def test_合规但超过1MiB_读回None(archive):
    big = [{"id": "va", "name": "va", "has_key": True, "models": ["m%07d" % i for i in range(100000)]}]
    _put(archive, {"saved_at": _stamp(), "providers": big})
    assert archive.stat().st_size > 1024 * 1024
    assert svc._load_catalog() is None


def test_存档是软链_写完路径成了普通文件_软链指向的文件一字不变(archive, tmp_path):
    """软链指向的就是同一份、刚存的清单：跟着软链读的话会当作"没变、不到 10 分钟"而不写，软链就留下了。"""
    target = tmp_path / "elsewhere.json"
    _put(target, {"saved_at": _stamp(timedelta(minutes=-1)), "providers": CATALOG})
    before = target.read_bytes()
    archive.parent.mkdir(parents=True)
    archive.symlink_to(target)
    svc._save_catalog(CATALOG)
    assert not archive.is_symlink() and archive.is_file()
    assert stat.S_IMODE(os.stat(archive).st_mode) == 0o600
    assert target.read_bytes() == before
    assert svc._load_catalog()[0] == CATALOG
