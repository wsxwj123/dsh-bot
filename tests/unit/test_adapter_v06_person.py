# -*- coding: utf-8 -*-
"""约定 v0.6 5.2：bots() 每条的 person，只看 bot 自己 yml 里的键，与 config_sources.person_id 同一口径。"""
import pytest

from moments import adapter_service as svc


def _bot(cfg, bot_id="bot4"):
    return {"id": bot_id, "cfg": cfg}


@pytest.mark.parametrize("cfg, want", [
    ({"life_config": "~/.dsh-bot/life/chenlulu.yml"}, "chenlulu"),
    ({"life_config": "  /tmp/x/wenya.yml  "}, "wenya"),
    ({"life_id": "lulu", "life_config": "~/.dsh-bot/life/chenlulu.yml"}, "lulu"),
    ({}, "bot4"),
    ({"life_config": ""}, "bot4"),
    ({"life_config": 3}, "bot4"),
    ({"life_id": "  "}, "bot4"),
])
def test_person(cfg, want):
    assert svc._person(_bot(cfg)) == want


def test_person_不打开life文件(tmp_path):
    life = tmp_path / "chenlulu.yml"
    life.write_text("life_id: 别的名字\n", encoding="utf-8")
    assert svc._person(_bot({"life_config": str(life)})) == "chenlulu"
