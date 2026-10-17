# -*- coding: utf-8 -*-
"""适配服务验收：供应商清单存档（provider-catalog-snapshot）。

依据只有一份：`.devflow/provider-catalog-snapshot-接口.md`（对外行为变化、错误契约、行为清单 1 到 13）。
不读实现。只经适配服务的 HTTP 入口发请求（GET /adapter/v1/providers、POST /adapter/v1/bots/<id>/provider），
并读写接口文件点名的两个文件：存档 `<DSH_BOT_HOME>/state/provider-catalog.json`、bot 的配置文件。

名词照接口文件："bot 在跑"是配置里启用、网关在应答；"bot 全停"是没有这样的 bot。
本文件让 bot 全停的办法：把配置改成停用，同时关掉它的假网关（两样都做，按哪条判都是全停）。
"先在有 bot 在跑时读一次"一律走对外接口，存档由服务自己写；只有"存档坏了""某一项缺 models"
这类服务自己不会写出来的场景，才手写存档文件。

用例名以"行为N"开头，N 是接口文件行为清单的编号；以"错误契约"开头的是清单没单列、
但"错误契约"一节写了的那一格。假网关清单的归并规则（停用的不列、has_key 的映射、models 取 choices）
沿用 test_adapter_read.py 已验的那套，这里只验"回的清单与网关报的一致"。
"""
import datetime
import json
import os
import re
import time

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
UTC_STAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
ITEM_KEYS = ("id", "name", "has_key", "models")      # 清单每一项的四个键
ARCHIVE_KEYS = {"saved_at", "providers"}             # 存档文件顶层的两个键
SWITCH_TO_B = {"provider_id": au.VENDOR_B, "model": au.VENDOR_B_MODELS[0]}
NEW_VENDOR = "fake-vendor-c"                         # 行为 11：在跑的 bot 后来多报出来的一家
NEW_VENDOR_MODELS = ["vc-one", "vc-two"]
GARBLED_REPLY = "<html><body>gateway is confused</body></html>"


# ---------------------------------------------------------------- 小件
def archive_path(adapter):
    """接口文件：存档在 <DSH_BOT_HOME>/state/provider-catalog.json。"""
    return adapter.sandbox.dsh_home / "state" / "provider-catalog.json"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_utc(stamp):
    return datetime.datetime.strptime(stamp, UTC_FORMAT).replace(tzinfo=datetime.timezone.utc)


def item(vendor, models, has_key=True):
    """清单里的一项（四个键）。"""
    return {"id": vendor, "name": vendor, "has_key": has_key, "models": list(models)}


def four_keys(entry):
    return {k: entry.get(k) for k in ITEM_KEYS}


def hand_archive(providers):
    """手写存档的内容：两个键 saved_at、providers。saved_at 用此刻的 UTC 时间，写法同 cached_at。"""
    return {"saved_at": utc_now().strftime(UTC_FORMAT), "providers": providers}


def put_archive(adapter, content):
    """手写一份存档，权限 0600。content 是 str 就原样写，别的按 JSON 写。"""
    path = archive_path(adapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def stop_all(bot, gw):
    """让 bot 全停：配置改成停用，同时关掉它的假网关。"""
    au.set_cfg(bot, enabled=False)
    gw.stop()


def read_once_then_stop_all(adapter):
    """行为 6、7、8 共用的开头：bot 在跑时 GET /providers 一次（存档由服务自己写），再让 bot 全停。

    返回这只 botalpha（后面要给它改供应商、读它的配置文件）。
    """
    gw = adapter.model_gateway()
    bot = adapter.running_bot("botalpha", gw)
    adapter.ok(adapter.call("GET", au.PROVIDERS))
    stop_all(bot, gw)
    return bot


@pytest.fixture
def local_zone_plus8():
    """本进程的本地时区钉成东八区：服务若把本地时间当 UTC 写进 cached_at，在任何机器上都会差 8 小时。"""
    if not hasattr(time, "tzset"):          # 没有 tzset 的平台，退化成按机器本来的时区测
        yield
        return
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


# ---------------------------------------------------------------- 供应商清单
def test_行为01_有一只bot在跑_供应商清单回200_与网关报的一致_不带cached_at(adapter):
    gw = adapter.model_gateway()
    adapter.running_bot("botalpha", gw)
    body = adapter.ok(adapter.call("GET", au.PROVIDERS))
    assert "cached_at" not in body, "有 bot 在跑时回包不该带 cached_at：%r" % (body,)
    got = {p["id"]: four_keys(p) for p in body["providers"]}
    want = {au.VENDOR_A: item(au.VENDOR_A, au.VENDOR_A_MODELS, has_key=True),
            au.VENDOR_B: item(au.VENDOR_B, au.VENDOR_B_MODELS, has_key=False)}
    assert len(body["providers"]) == 2 and got == want, \
        "清单应与网关报的一致（A 有密钥、B 缺密钥；停用的那家不列）：%r" % (body["providers"],)
    assert body["default"] == {"label": au.DEFAULT_LABEL}, body["default"]


def test_行为02_在跑时读过一次再全停_供应商清单回200_清单与之前相同_带UTC的cached_at(
        adapter, local_zone_plus8):
    gw = adapter.model_gateway()
    bot = adapter.running_bot("botalpha", gw)
    read_from = utc_now()
    first = adapter.ok(adapter.call("GET", au.PROVIDERS))
    read_to = utc_now()
    time.sleep(2.5)         # 拉开"读到清单的时刻"和"现在"，才分得出 cached_at 记的是哪一个
    stop_all(bot, gw)
    body = adapter.ok(adapter.call("GET", au.PROVIDERS))
    assert body["providers"] == first["providers"], \
        "全停后回的清单应与之前那次完全相同：\n之前 %r\n现在 %r" % (first["providers"], body["providers"])
    assert body["default"] == first["default"] == {"label": au.DEFAULT_LABEL}, body["default"]
    assert set(body) == set(first) | {"cached_at"}, \
        "回包应恰好比在跑时多一个 cached_at 键：在跑时 %r，现在 %r" % (sorted(first), sorted(body))
    cached_at = body["cached_at"]
    assert isinstance(cached_at, str) and UTC_STAMP_RE.fullmatch(cached_at), \
        "cached_at 应是 YYYY-MM-DDTHH:MM:SSZ 形式的字符串：%r" % (cached_at,)
    stamp = parse_utc(cached_at)
    lower = read_from.replace(microsecond=0)
    upper = read_to + datetime.timedelta(seconds=1)
    assert lower <= stamp <= upper, \
        "cached_at 应是读到清单那一刻的 UTC 时间（%s 到 %s 之间），实际 %s" % (
            lower.strftime(UTC_FORMAT), read_to.strftime(UTC_FORMAT), cached_at)


def test_行为03_从没读过清单_bot全停_供应商清单回503_unavailable(adapter):
    adapter.bot("botalpha", enabled=False)
    adapter.expect_error(adapter.call("GET", au.PROVIDERS), 503, "unavailable")


def test_行为04_存档内容不是JSON_bot全停_供应商清单回503_unavailable(adapter):
    adapter.bot("botalpha", enabled=False)
    put_archive(adapter, "this is not json {{{ ]]]")
    adapter.expect_error(adapter.call("GET", au.PROVIDERS), 503, "unavailable")


BAD_SHAPES = {
    "providers是对象不是数组": {au.VENDOR_A: item(au.VENDOR_A, au.VENDOR_A_MODELS)},
    "某一项缺models": [item(au.VENDOR_A, au.VENDOR_A_MODELS),
                       {"id": au.VENDOR_B, "name": au.VENDOR_B, "has_key": False}],
}


@pytest.mark.parametrize("providers", list(BAD_SHAPES.values()), ids=list(BAD_SHAPES))
def test_行为05_存档形状不对_bot全停_供应商清单回503_unavailable(adapter, providers):
    adapter.bot("botalpha", enabled=False)
    put_archive(adapter, hand_archive(providers))
    adapter.expect_error(adapter.call("GET", au.PROVIDERS), 503, "unavailable")


# ---------------------------------------------------------------- 停着的 bot 改供应商：按存档核对
def test_行为06_有存档_bot全停_存档里有的供应商和模型_回200_queued_配置brain段变成新值(adapter):
    bot = read_once_then_stop_all(adapter)
    body = adapter.ok(adapter.call("POST", au.provider_url("botalpha"), SWITCH_TO_B))
    assert body.get("queued") is True, "停着的 bot 改供应商应回 queued 为 true：%r" % (body,)
    brain = au.read_cfg(bot).get("brain") or {}
    assert (brain.get("provider"), brain.get("model")) == (au.VENDOR_B, au.VENDOR_B_MODELS[0]), \
        "配置文件 brain 段应变成新值 %s/%s，实际 %r" % (au.VENDOR_B, au.VENDOR_B_MODELS[0], brain)


def test_行为07_有存档_bot全停_供应商不在存档里_回404_unknown_provider_配置不变(adapter):
    bot = read_once_then_stop_all(adapter)
    before = bot.yml.read_bytes()
    payload = {"provider_id": "no-such-provider-zz9", "model": "whatever"}
    adapter.expect_error(adapter.call("POST", au.provider_url("botalpha"), payload),
                         404, "unknown_provider")
    assert bot.yml.read_bytes() == before, "回了 404，配置文件却被改了"


def test_行为08_有存档_bot全停_这家没有这个模型_回422_invalid_配置不变(adapter):
    """模型故意选"别家才有的"：只在整份存档里找模型名、不分供应商的核对法会在这里露馅。"""
    bot = read_once_then_stop_all(adapter)
    before = bot.yml.read_bytes()
    payload = {"provider_id": au.VENDOR_B, "model": au.VENDOR_A_MODELS[1]}
    adapter.expect_error(adapter.call("POST", au.provider_url("botalpha"), payload), 422, "invalid")
    assert bot.yml.read_bytes() == before, "回了 422，配置文件却被改了"


def test_行为09_没有存档_bot全停_停着的bot改供应商_回503_unavailable_配置不变(adapter):
    bot = adapter.bot("botalpha", enabled=False)
    before = bot.yml.read_bytes()
    adapter.expect_error(adapter.call("POST", au.provider_url("botalpha"), SWITCH_TO_B),
                         503, "unavailable")
    assert bot.yml.read_bytes() == before, "回了 503，配置文件却被改了"


def test_错误契约_存档形状不对_bot全停_停着的bot改供应商_回503_unavailable_配置不变(adapter):
    """行为清单只列了"没有存档"（行为 9）；"错误契约"一节写明存档形状不对时同样回 503。
    缺 models 的是另一家（A），要改去的这家（B）本身完好：整份存档都不能信，不能只看 B 那一项。"""
    bot = adapter.bot("botalpha", enabled=False)
    put_archive(adapter, hand_archive([
        item(au.VENDOR_B, au.VENDOR_B_MODELS, has_key=False),
        {"id": au.VENDOR_A, "name": au.VENDOR_A, "has_key": True},
    ]))
    before = bot.yml.read_bytes()
    adapter.expect_error(adapter.call("POST", au.provider_url("botalpha"), SWITCH_TO_B),
                         503, "unavailable")
    assert bot.yml.read_bytes() == before, "回了 503，配置文件却被改了"


# ---------------------------------------------------------------- 存档文件本身
def test_行为10_存档文件权限0600_只有saved_at和providers_每项只有四个键_不含密钥和接口地址(adapter):
    gw = adapter.model_gateway()
    adapter.running_bot("botalpha", gw)
    body = adapter.ok(adapter.call("GET", au.PROVIDERS))
    path = archive_path(adapter)
    assert path.is_file(), "bot 在跑时读过清单，却没有存档文件 state/%s" % path.name
    assert au.mode_of(path) == 0o600, "存档权限应为 0600，实际 %o" % au.mode_of(path)
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError:
        raise AssertionError("存档文件不是 JSON：开头 %r" % raw[:80])
    assert isinstance(data, dict) and set(data) == ARCHIVE_KEYS, \
        "存档应是只有 saved_at、providers 两个键的对象：%r" % (data if not isinstance(data, dict) else sorted(data),)
    assert isinstance(data["providers"], list) and data["providers"], "存档的 providers 应是非空数组"
    for entry in data["providers"]:
        assert isinstance(entry, dict) and set(entry) == set(ITEM_KEYS), \
            "存档里每一项应只有 id、name、has_key、models 四个键：%r" % (entry,)
    assert data["providers"] == body["providers"], \
        "存档里的清单应就是刚读到的那份：\n存档 %r\n回包 %r" % (data["providers"], body["providers"])
    for word in au.FAKE_KEY_ENVS + [au.FAKE_ENDPOINT_HOST, "://"] + list(au.SECRETS.values()):
        assert word not in raw, "存档里出现了不该存的东西：%r" % word
    hit = re.search(r"[A-Z][A-Z0-9_]*_KEY\b", raw)
    assert hit is None, "存档里带了密钥的环境变量名：%r" % (hit and hit.group(0))


def test_行为11_在跑的清单变了_再读一次_然后全停_回的是变了以后的清单(adapter):
    gw = adapter.model_gateway()
    bot = adapter.running_bot("botalpha", gw)
    first = adapter.ok(adapter.call("GET", au.PROVIDERS))
    reply = gw.state["reply"]                # 假网关下一次要回的清单：多一家
    reply["providers"].append(au.provider_entry(NEW_VENDOR, count=len(NEW_VENDOR_MODELS), key="ok"))
    reply["choices"].extend({"provider": NEW_VENDOR, "model": m} for m in NEW_VENDOR_MODELS)
    second = adapter.ok(adapter.call("GET", au.PROVIDERS))
    assert NEW_VENDOR not in [p["id"] for p in first["providers"]], first["providers"]
    assert NEW_VENDOR in [p["id"] for p in second["providers"]], \
        "有 bot 在跑时应回网关当下报的清单（该有新加的 %s）：%r" % (NEW_VENDOR, second["providers"])
    stop_all(bot, gw)
    body = adapter.ok(adapter.call("GET", au.PROVIDERS))
    assert "cached_at" in body, "全停后的回包应带 cached_at：%r" % (body,)
    assert body["providers"] == second["providers"], \
        "全停后回的应是变了以后的清单，不是第一次存的那份：\n应为 %r\n实际 %r" % (
            second["providers"], body["providers"])


# ---------------------------------------------------------------- 不拿存档顶替、只改强度
def test_行为12_有存档_同时有bot在跑但网关回认不出的内容_回500_internal_不拿存档顶替(adapter):
    gw = adapter.model_gateway()
    adapter.running_bot("botalpha", gw)
    adapter.ok(adapter.call("GET", au.PROVIDERS))               # 在跑时读一次，留下存档
    gw.on("GET", au.GW_MODEL, 200, GARBLED_REPLY)               # 网关还在应答，但回的东西认不出
    body = adapter.expect_error(adapter.call("GET", au.PROVIDERS), 500, "internal")
    assert "providers" not in body and "cached_at" not in body, \
        "网关回的东西认不出，不该拿存档顶替：%r" % (body,)


def test_行为13_没有存档_bot全停_停着的bot只改强度_回200_queued_不需要清单(adapter):
    adapter.bot("botalpha", enabled=False)
    body = adapter.ok(adapter.call("POST", au.provider_url("botalpha"), {"effort": "high"}))
    assert body.get("queued") is True, "只改强度应回 queued 为 true：%r" % (body,)
