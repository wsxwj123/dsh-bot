# -*- coding: utf-8 -*-
"""create-life-file 白盒：验收测试碰不到的几处。

- ``life_settings.planned_dir``：目录还没建出来也回写法；写法落在禁区里回 None（``allowed_dir`` 行为不变）。
- ``hub_addbot._life_text``：最小生活设定能被解析成只有 id、display_name 的映射。
- ``hub_addbot.lay_down``：生活设定目录是指进禁区的软链时不生成文件、配置不写 life_config，bot 照常建成。
"""
import os

import pytest
import yaml

from moments import hub_addbot
from moments import life_settings as life


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, dsh_home = tmp_path / "home", tmp_path / "dsh-home"
    (home / ".claude").mkdir(parents=True)
    dsh_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DSH_BOT_HOME", str(dsh_home))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh_home / "configs"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs-legacy"))
    monkeypatch.delenv("DSH_CONSOLE_LIFE_DIR", raising=False)
    return home, dsh_home


def test_planned_dir_目录不在也回写法_allowed_dir仍回None(env):
    _home, dsh_home = env
    want = os.path.join(str(dsh_home), "life")
    assert life.planned_dir() == want
    assert life.allowed_dir() is None
    assert hub_addbot.life_target("botnew") == (want, os.path.join(want, "botnew.yml"))
    os.mkdir(want)
    assert life.allowed_dir()[0] == want


def test_planned_dir_写法落在禁区_回None_不生成(env, monkeypatch):
    home, _dsh_home = env
    monkeypatch.setenv("DSH_CONSOLE_LIFE_DIR", str(home / ".claude" / "life"))
    assert life.planned_dir() is None
    assert hub_addbot.life_target("botnew") is None
    monkeypatch.setenv("DSH_CONSOLE_LIFE_DIR", "relative/life")
    assert life.planned_dir() is None


def test_最小生活设定_只有id与display_name(env):
    text = hub_addbot._life_text({"bot_id": "botnew", "display_name": "小雨: null", "person": "lucy"})
    assert yaml.safe_load(text) == {"id": "lucy", "display_name": "小雨: null"}
    text = hub_addbot._life_text({"bot_id": "botnew", "display_name": "x"})
    assert yaml.safe_load(text)["id"] == "botnew"


def test_lay_down_生活设定目录软链进禁区_不生成也不写life_config(env, monkeypatch):
    home, dsh_home = env
    target = home / ".claude" / "life"
    target.mkdir()
    os.symlink(str(target), str(dsh_home / "life"))     # 写法合格，真实路径在禁区
    fields = {"bot_id": "botnew", "token": "1:x", "chat_id": 1, "port": 40001, "display_name": "x",
              "persona": "p", "fish_voice_id": None, "fish_api_key": None}
    channel = os.path.join(str(dsh_home), "bots", "botnew", "channel")
    fields["yml_text"] = hub_addbot._render_yml(fields, channel, "00000000-0000-4000-8000-000000000000")
    hub_addbot.lay_down(fields, port_check=lambda: None)
    cfg = yaml.safe_load((dsh_home / "configs" / "botnew.yml").read_text(encoding="utf-8"))
    assert "life_config" not in cfg
    assert "life_file" not in fields
    assert os.listdir(str(target)) == []
