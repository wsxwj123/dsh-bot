# -*- coding: utf-8 -*-
"""适配服务验收：reset-provider（清掉单独指定，回到配置文件里那一组）。

依据：`.devflow/reset-provider-接口.md` 第 1、2、3 节。只经适配接口（HTTP）与假网关
（外部服务替身）验证；停着的 bot 的落点按接口第 3 节写明的字段（brain 里的
provider / model / reasoning_effort / chosen_at）读回配置确认。不读实现。
"""
import json

import pytest

from . import adapter_util as au
from . import botops_util as u
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

RUNNING = "botalpha"
STOPPED = "botbravo"

#: 与配置文件里那一组不同的单独指定（换到了另一家）
OTHER_PAIR = (au.VENDOR_B, au.VENDOR_B_MODELS[0])
#: 同一家供应商、只换了模型（VENDOR_A 就是配置文件里写的那一家）
SAME_VENDOR_OTHER_MODEL = (au.CFG_PROVIDER, au.VENDOR_A_MODELS[1])
#: 停着的 bot 配置里预先放一个醒目的旧 chosen_at，"换成新值"才结论明确
CHOSEN_SENTINEL = "1999-01-01T00:00:00Z"
RESPONSE_KEYS = {"ok", "applied", "queued", "new_segment", "message"}


class ModelGateway:
    """假网关：应答 reset 用到的四条请求，并按契约处理 `spec: "default"`。

    - `GET /v1/model` 回 current / config（配置文件里那一组）；
    - `POST /v1/model`：`{"spec": "default"}` 把 current 换回 config；`"供应商/模型"`
      把 current 换成那一组；
    - `GET /v1/effort` / `POST /v1/effort`：读写单独指定的强度；`reject_effort` 里的值
      回 422（用来演"重置后改强度被网关拒绝"）。
    """

    def __init__(self, adapter, *, current=au.CFG_PAIR, config=au.CFG_PAIR,
                 effort=None, reject_effort=()):
        self.current = tuple(current)
        self.config = tuple(config)
        self.effort = effort
        self.reject_effort = set(reject_effort)
        self.gw = adapter.gateway()
        self.gw.on_fn("GET", au.GW_MODEL, lambda rec: (200, self._model_reply()))
        self.gw.on_fn("POST", au.GW_MODEL, self._post_model)
        self.gw.on_fn("GET", au.GW_EFFORT, lambda rec: (200, {"ok": True, "current": self.effort}))
        self.gw.on_fn("POST", au.GW_EFFORT, self._post_effort)

    @property
    def port(self):
        return self.gw.port

    def _model_reply(self):
        return au.model_reply(current=self.current, config=self.config)

    def _post_model(self, rec):
        parsed = rec["json"] if isinstance(rec["json"], dict) else {}
        spec = parsed.get("spec")
        if spec == "default":
            self.current = self.config
        elif isinstance(spec, str) and "/" in spec:
            self.current = tuple(spec.split("/", 1))
        else:
            return 400, {"ok": False}
        return 200, {"ok": True,
                     "current": {"provider": self.current[0], "model": self.current[1]}}

    def _post_effort(self, rec):
        parsed = rec["json"] if isinstance(rec["json"], dict) else {}
        effort = parsed.get("effort")
        if effort in self.reject_effort:
            return 422, {"ok": False}
        self.effort = effort
        return 200, {"ok": True}

    def writes(self):
        return self.gw.writes()


def _running(adapter, **kw):
    """一只正在运行的 bot：心跳新鲜，网关是上面那个会应答的 ModelGateway。"""
    gw = ModelGateway(adapter, **kw)
    bot = adapter.running_bot(RUNNING, gw)
    return gw, bot


def _row(adapter, bot_id):
    return au.rows_by_id(adapter)[bot_id]


def _assert_provider_response(body, *, applied):
    """回包形状如约：恰好五个键，applied / queued 恰有一个为 true。"""
    assert set(body) == RESPONSE_KEYS, "回包形状应为约定的五个键：%r" % (body,)
    assert body["ok"] is True, body
    assert type(body["applied"]) is bool and type(body["queued"]) is bool, body
    assert body["applied"] is applied and body["queued"] is (not applied), body
    assert type(body["new_segment"]) is bool, body
    assert isinstance(body["message"], str) and body["message"].strip(), body


def _seed_chosen_at(bot, value=CHOSEN_SENTINEL):
    cfg = au.read_cfg(bot)
    cfg.setdefault("brain", {})["chosen_at"] = value
    bot.yml.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _config_without_chosen_at(cfg):
    """整份配置去掉 brain.chosen_at 之后的样子，用来验"不改别的键"。"""
    out = dict(cfg)
    brain = dict(out.get("brain") or {})
    brain.pop("chosen_at", None)
    out["brain"] = brain
    return out


# ============================================================ 行为 1

def test_meta_能力表_reset_provider为真(adapter):
    body = adapter.ok(adapter.call("GET", au.META))
    assert body["capabilities"]["bot.reset_provider"] is True


# ============================================================ 行为 2

def test_在跑的bot_有单独指定_重置回到配置那组_网关收到default(adapter):
    gw, bot = _running(adapter, current=OTHER_PAIR)
    cfg = au.read_cfg(bot)
    want_provider = cfg["brain"]["provider"]
    want_model = cfg["brain"]["model"]
    assert _row(adapter, RUNNING)["provider_source"] == "override", "前提：这只 bot 有单独指定"

    body = adapter.ok(adapter.call("POST", au.provider_url(RUNNING), {"reset": True}))

    _assert_provider_response(body, applied=True)
    assert body["queued"] is False, body
    writes = gw.writes()
    assert [(w["method"], w["path"]) for w in writes] == [("POST", au.GW_MODEL)], \
        "重置应恰好向网关发一次 POST /v1/model：%r" % gw.gw.requests
    assert writes[0]["json"] == {"spec": "default"}, writes[0]["body"]
    row = _row(adapter, RUNNING)
    assert row["provider_source"] == "default", row
    assert (row["provider"]["id"], row["provider"]["model"]) == (want_provider, want_model), row


# ============================================================ 行为 3

def test_在跑的bot_本来就在默认_重置成功_new_segment为假(adapter):
    gw, bot = _running(adapter)          # current 与 config 相同
    body = adapter.ok(adapter.call("POST", au.provider_url(RUNNING), {"reset": True}))

    _assert_provider_response(body, applied=True)
    assert body["new_segment"] is False, "本来就在默认那一组，new_segment 应为 false：%r" % (body,)
    assert _row(adapter, RUNNING)["provider_source"] == "default"


# ============================================================ 行为 4

def test_在跑的bot_从另一家供应商重置_new_segment为真(adapter):
    _running(adapter, current=OTHER_PAIR)
    body = adapter.ok(adapter.call("POST", au.provider_url(RUNNING), {"reset": True}))
    _assert_provider_response(body, applied=True)
    assert body["new_segment"] is True, \
        "从 %s 回到 %s 是换供应商，new_segment 应为 true：%r" % (
            au.VENDOR_B, au.CFG_PROVIDER, body)


def test_在跑的bot_同一家只换模型_重置_new_segment为假(adapter):
    _running(adapter, current=SAME_VENDOR_OTHER_MODEL)
    body = adapter.ok(adapter.call("POST", au.provider_url(RUNNING), {"reset": True}))
    _assert_provider_response(body, applied=True)
    assert body["new_segment"] is False, \
        "两家都是 %s，只是模型变了，new_segment 应为 false：%r" % (au.CFG_PROVIDER, body)


# ============================================================ 行为 5

def test_在跑的bot_重置并改强度_网关先default后high_两项都生效(adapter):
    gw, bot = _running(adapter, current=OTHER_PAIR, effort="low")
    body = adapter.ok(adapter.call("POST", au.provider_url(RUNNING),
                                   {"reset": True, "effort": "high"}))

    _assert_provider_response(body, applied=True)
    writes = gw.writes()
    assert [(w["method"], w["path"]) for w in writes] == [("POST", au.GW_MODEL), ("POST", au.GW_EFFORT)], \
        "网关应先收到 default、再收到强度 high：%r" % gw.gw.requests
    assert writes[0]["json"] == {"spec": "default"}, writes[0]["body"]
    assert writes[1]["json"].get("effort") == "high", writes[1]["body"]
    row = _row(adapter, RUNNING)
    assert row["provider_source"] == "default", row
    assert row["provider"]["model"] == au.CFG_MODEL, row
    assert row["provider"]["effort"] == "high", "单独指定的强度应变成 high：%r" % (row,)


# ============================================================ 行为 6

def test_停着的bot_重置_写进配置_provider与model不动_chosen_at更新(adapter):
    gw = adapter.gateway()
    bot = adapter.bot(STOPPED, enabled=False, gw_port=gw.port)
    _seed_chosen_at(bot)
    before = au.read_cfg(bot)

    body = adapter.ok(adapter.call("POST", au.provider_url(STOPPED), {"reset": True}))

    _assert_provider_response(body, applied=False)
    assert body["queued"] is True, body
    after = au.read_cfg(bot)
    assert after["brain"]["provider"] == au.CFG_PROVIDER, after
    assert after["brain"]["model"] == au.CFG_MODEL, after
    chosen = after["brain"].get("chosen_at")
    assert isinstance(chosen, str) and chosen and chosen != CHOSEN_SENTINEL, \
        "chosen_at 应换成新值：%r" % (after,)
    assert _config_without_chosen_at(after) == _config_without_chosen_at(before), \
        "重置改了 brain.chosen_at 之外的键：前 %r 后 %r" % (before, after)
    assert gw.requests == [], "停着的 bot 不需要网关，却去找了它：%r" % gw.requests


# ============================================================ 行为 7

def test_停着的bot_重置并改强度_reasoning_effort为high_chosen_at更新(adapter):
    gw = adapter.gateway()
    bot = adapter.bot(STOPPED, enabled=False, gw_port=gw.port)
    _seed_chosen_at(bot)

    body = adapter.ok(adapter.call("POST", au.provider_url(STOPPED),
                                   {"reset": True, "effort": "high"}))

    _assert_provider_response(body, applied=False)
    assert body["queued"] is True, body
    after = au.read_cfg(bot)
    assert after["brain"].get("reasoning_effort") == "high", after
    chosen = after["brain"].get("chosen_at")
    assert isinstance(chosen, str) and chosen and chosen != CHOSEN_SENTINEL, \
        "chosen_at 应换成新值：%r" % (after,)
    assert after["brain"]["provider"] == au.CFG_PROVIDER and after["brain"]["model"] == au.CFG_MODEL, after
    assert gw.requests == [], "停着的 bot 不需要网关，却去找了它：%r" % gw.requests


# ============================================================ 行为 8

def test_重置与provider_id_model同时出现_回422_invalid_零写入(adapter):
    gw, bot = _running(adapter)
    frozen = u.freeze_roots(bot.yml)
    payload = {"reset": True, "provider_id": "p1", "model": "m1"}

    adapter.expect_error(adapter.call("POST", au.provider_url(RUNNING), payload),
                         422, "invalid")

    assert gw.writes() == [], "入参不合规却往网关写了东西：%r" % gw.gw.writes()
    u.assert_roots_untouched(frozen)


# ============================================================ 行为 9

@pytest.mark.parametrize("payload", [{"reset": False}, {"reset": "yes"}, {"reset": 1}],
                         ids=["reset为false", "reset是字符串yes", "reset是数字1"])
def test_重置不是布尔或为假_回422_invalid_零写入(adapter, payload):
    gw, bot = _running(adapter)
    frozen = u.freeze_roots(bot.yml)

    adapter.expect_error(adapter.call("POST", au.provider_url(RUNNING), payload),
                         422, "invalid")

    assert gw.writes() == [], "入参不合规却往网关写了东西：%r" % gw.gw.writes()
    u.assert_roots_untouched(frozen)


# ============================================================ 行为 10

def test_在跑的bot_网关连不上_重置回503_unavailable_配置零写入(adapter):
    bot = adapter.bot(RUNNING, gw_port=au.free_port())      # 端口没人听
    adapter.heartbeat.tick(RUNNING, pid=4242)
    frozen = u.freeze_roots(bot.yml)

    adapter.expect_error(adapter.call("POST", au.provider_url(RUNNING), {"reset": True}),
                         503, "unavailable")

    u.assert_roots_untouched(frozen)


# ============================================================ 行为 11

def test_在跑的bot_重置后改强度被网关拒绝_恢复原单独指定与强度_仍回422(adapter):
    gw, bot = _running(adapter, current=OTHER_PAIR, effort="low", reject_effort=("high",))

    resp = adapter.call("POST", au.provider_url(RUNNING), {"reset": True, "effort": "high"})

    body = au.body_of(resp)
    assert resp.status_code == 422, "改强度被网关拒绝，应仍按 422 返回：%s %r" % (
        resp.status_code, body)
    assert body.get("ok") is False, body
    adapter.human_message(body.get("message"))
    row = _row(adapter, RUNNING)
    assert row["provider_source"] == "override", "重置失败后应恢复原来的单独指定：%r" % (row,)
    assert (row["provider"]["id"], row["provider"]["model"]) == OTHER_PAIR, row
    assert row["provider"]["effort"] == "low", "原来的强度 low 应恢复：%r" % (row,)
    assert gw.current == OTHER_PAIR and gw.effort == "low", \
        "网关上的现状应恢复到重置前：current=%r effort=%r" % (gw.current, gw.effort)
