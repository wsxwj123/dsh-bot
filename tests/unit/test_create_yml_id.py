# -*- coding: utf-8 -*-
"""新建 bot 落盘的配置里 `id` 带双引号（INTERFACE-新系统bot运维 §7，第 7 稿）。

`null`、`true`、`false`、`yes`、`no`、`on`、`off` 都是形状合法的 bot id。裸写进 YAML 的话
读回来是空值或布尔值，网关与安装脚本拿到的就不是这只的名字。带上双引号，与
`display_name` 同一种写法，任何形状合法的名字读回来都是原来那个字符串。

直接调 `hub_addbot._render_yml`，不落盘、不起进程。
"""
import pytest
import yaml

from moments import config_sources, hub_addbot

YAML_WORDS = ["null", "true", "false", "yes", "no", "on", "off"]


@pytest.fixture(autouse=True)
def _empty_roots(tmp_path, monkeypatch):
    """取 namespace 时要扫两套配置根：都指到 tmp 里的空目录。"""
    for name, env in (("legacy", "HUB_CONFIGS_DIR"), ("dsh", "HUB_CONFIGS_DSH_DIR")):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(env, str(tmp_path / name))


def _render(bot_id, tmp_path):
    return hub_addbot._render_yml(
        {"bot_id": bot_id, "display_name": "新来的", "chat_id": 123, "port": 17990},
        str(tmp_path / "dsh-home" / "bots" / bot_id / "channel"))


def test_首行是带双引号的id_与display_name同一种写法(tmp_path):
    lines = _render("xbot", tmp_path).splitlines()
    assert lines[0] == 'id: "xbot"'
    assert lines[1] == 'display_name: "新来的"'


@pytest.mark.parametrize("word", YAML_WORDS)
def test_YAML里有特殊含义的词当名字_读回来仍是这个字符串(word, tmp_path):
    assert config_sources.BOT_ID_RE.match(word), "前提：这个词是形状合法的 bot id"
    assert yaml.safe_load("id: %s\n" % word)["id"] != word, "前提：裸写的话读回来不是它"
    got = yaml.safe_load(_render(word, tmp_path))["id"]
    assert isinstance(got, str) and got == word


def test_其余几个键与顺序不变(tmp_path):
    loaded = yaml.safe_load(_render("xbot", tmp_path))
    assert list(loaded) == ["id", "display_name", "bot_channel_path", "chat_id",
                            "dispatcher_port", "namespace_uuid"]
    assert (loaded["chat_id"], loaded["dispatcher_port"]) == (123, 17990)
