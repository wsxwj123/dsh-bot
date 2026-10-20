# -*- coding: utf-8 -*-
"""行为 8：hub 转发 POST /hub/api/dsh-model/provider/<名字>/models 的 set_input。

搭法与 test_console_pages_hub.py 相同：真实的适配服务 + 假网关（RecordingGateway）。
管理台只负责把 action/input 原样转给网关的 POST /v1/provider/model（带上路径里的供应商名），
并把网关的 200 回包原样交回。providers.json 的落盘由网关侧行为 1–3 覆盖。
"""
from urllib.parse import quote

from .adapter_util import (  # noqa: F401  adapter、console_guard 是夹具，导入进来才能用
    ADAPTER_TOKEN,
    adapter,
    console_guard,
)

MODELS = "/hub/api/dsh-model/provider/%s/models"
GW_MODEL = "/v1/provider/model"
VENDOR = "fake-vendor-b"
GW_TEXT = "已把 vb-small 的输入类型改成 text、image。"


def _hub(adapter, path, body):
    return adapter.client.open(
        path, method="POST",
        headers={"Authorization": "Bearer " + ADAPTER_TOKEN},
        json=body,
    )


def test_行为8_hub转发set_input_200_转发体带name_action_input_回包原样透传(adapter):
    gw = adapter.gateway()
    gw.on("POST", GW_MODEL, 200, {"ok": True, "text": GW_TEXT})
    adapter.bot("botalpha", gw_port=gw.port)

    r = _hub(adapter, MODELS % quote(VENDOR, safe=""),
             {"action": "set_input", "id": "vb-small", "input": ["text", "image"]})

    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    assert r.get_json() == {"ok": True, "text": GW_TEXT}, \
        "200 应原样透传网关（{ok,text}），实得 %r" % (r.get_json(),)

    hits = [x for x in gw.requests if x["method"] == "POST" and x["path"] == GW_MODEL]
    assert len(hits) == 1, [(x["method"], x["path"], x["json"]) for x in gw.requests]
    assert hits[0]["json"] == {"name": VENDOR, "action": "set_input",
                               "id": "vb-small", "input": ["text", "image"]}, \
        "转给网关的请求体应与直连网关一致（name 取自路径），实得 %r" % (hits[0]["json"],)
