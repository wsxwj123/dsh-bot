# -*- coding: utf-8 -*-
"""适配服务 v0.6、v0.7 里验收碰不到的内部逻辑（补充文件第 9 节）。

- 停用开关在 JSON 写法（流式映射）的配置里插 ``enabled``；
- 切进导入（``import_history.switch_in``）：没有水位线时的 30 天窗口、按水位线增量、坏文件与时限都在写账本之前抛；
- 网关健康接口回 503（真网关在有同跑冲突时就回 503）也照样读出 telegram 那一段；
- ``human_ts`` 形状对、日期不存在的也拒。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from moments import adapter_service as svc
from moments import hub_config
from scripts import import_history

CHAT = "5550001777"


def test_停用开关_流式写法的配置插入enabled_仍是合法YAML其余原样():
    text = json.dumps({"id": "botx", "brain": {"provider": "p", "model": "m"}})
    out = hub_config.set_scalar(text, hub_config.compose(text), "enabled", False, "bool")
    assert yaml.safe_load(out) == {"enabled": False, "id": "botx", "brain": {"provider": "p", "model": "m"}}
    assert out.endswith(text[1:]), "第一个键之后的字节应一字不动"
    block = "id: botx\nbrain:\n  model: m\n"
    out = hub_config.set_scalar(block, hub_config.compose(block), "enabled", False, "bool")
    assert out == "enabled: false\n" + block, "块写法照旧按行插在块首"


def _line(days_ago, text):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    tag = '<channel source="plugin:telegram:telegram" chat_id="%s" user="owner" ts="%s">\n%s\n</channel>' % (
        CHAT, ts, text)
    return json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": tag}},
                      ensure_ascii=False) + "\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path / "dsh"))
    return tmp_path


def _ledger(home):
    return home / "dsh" / "bots" / "botx" / "state" / "ledger.sqlite"


def test_切进导入_第一次导最近30天_之后只导水位线之后的_承诺去重_坏行跳过(home):
    sess = home / "s.jsonl"
    sess.write_text(_line(40, "四十天前") + "{坏行\n" + _line(2, "两天前"), encoding="utf-8")
    due = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    promises = home / ".promises.json"
    promises.write_text(json.dumps({"promises": [{"text": "明早叫醒", "due": due}]}), encoding="utf-8")
    assert import_history.switch_in("botx", CHAT, str(promises), [str(sess)]) == (1, 1)
    with open(sess, "a", encoding="utf-8") as f:
        f.write(_line(1, "一天前"))
    assert import_history.switch_in("botx", CHAT, str(promises), [str(sess)]) == (1, 0)
    assert import_history.switch_in("botx", CHAT, None, [str(sess)]) == (0, 0)


def test_切进导入_会话文件不是UTF8_抛出且账本一条不写(home):
    bad = home / "bad.jsonl"
    bad.write_bytes(_line(1, "好的一行").encode("utf-8") + b"\xff\xfe\n")
    with pytest.raises(UnicodeDecodeError):
        import_history.switch_in("botx", CHAT, None, [str(bad)])
    assert not _ledger(home).exists()


def test_切进导入_到了时限还没读完_抛ImportTimeout且账本一条不写(home):
    sess = home / "s.jsonl"
    sess.write_text(_line(1, "一天前"), encoding="utf-8")
    with pytest.raises(import_history.ImportTimeout):
        import_history.switch_in("botx", CHAT, None, [str(sess)], deadline=0.0)
    assert not _ledger(home).exists()


@pytest.mark.parametrize("code", [200, 503])
def test_健康接口_有毛病回503时照样读出telegram段_别的不带(monkeypatch, code):
    reply = {"ok": code == 200, "chats": [{"chat_id": CHAT}],
             "telegram": {"conflict": True, "username": "alpha_bot"}}
    monkeypatch.setattr(svc.gateway_client, "available", lambda channel: True)
    monkeypatch.setattr(svc.gateway_client, "_call", lambda *a, **kw: (code, reply))
    assert svc._gateway_health("/x/channel") == {"conflict": True, "username": "alpha_bot"}
    monkeypatch.setattr(svc.gateway_client, "_call", lambda *a, **kw: (401, {"error": "unauthorized"}))
    assert svc._gateway_health("/x/channel") == {}


def test_human_ts_形状对但日期不存在也拒():
    assert svc._human_ts("2026-10-14T08:00:00.000Z")
    for bad in ("2026-13-01T00:00:00.000Z", "2026-02-30T00:00:00.000Z", "2026-10-14T24:00:00.000Z"):
        assert not svc._human_ts(bad), bad
