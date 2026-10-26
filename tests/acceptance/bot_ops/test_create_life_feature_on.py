# -*- coding: utf-8 -*-
"""create-life-feature-on 验收：新建时勾了 life 且这次新建生成了生活设定文件，life 开关直接打开。

依据只有 .devflow/create-life-feature-on-接口.md（行为清单 2 到 6 条，编号对应用例名里的“清单N”；
第 1 条是 test_adapter_v11_create.py 里“全勾”那条的期望改动）。夹具与调用写法照抄 test_adapter_v11_create.py、
test_adapter_v11_features.py、test_create_life_file.py。断言信息只取单个键，不打印 env 与请求体。
"""
import os

import pytest
import yaml

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具
from .test_adapter_v10_create import NEW, good_body

KEYS = ["voice", "image", "stickers", "moments", "proactive", "life", "relationship", "groups", "nsfw"]


@pytest.fixture
def life_dir(adapter, monkeypatch):
    """生活设定目录 = <DSH_BOT_HOME>/life，经 DSH_CONSOLE_LIFE_DIR 指定；此刻不存在。"""
    d = adapter.sandbox.dsh_home / "life"
    monkeypatch.setenv("DSH_CONSOLE_LIFE_DIR", str(d))
    assert not os.path.lexists(str(d))
    return d


def new_cfg(adapter):
    return yaml.safe_load((adapter.sandbox.dsh_cfg / ("%s.yml" % NEW)).read_text(encoding="utf-8"))


def create_ok(adapter, **over):
    return adapter.ok(adapter.call("POST", au.BOTS, good_body(**over)))


def test_清单2_只勾life新建_life为真_其余八个为假_无跳过项_配置为真_文件在(adapter, telegram, bun, life_dir):
    got = create_ok(adapter, features=["life"])
    assert got["features"] == dict(dict.fromkeys(KEYS, False), life=True), got["features"]
    assert got["features_skipped"] == [], got["features_skipped"]
    assert new_cfg(adapter)["features"] == dict(dict.fromkeys(KEYS, False), life=True)
    path = life_dir / ("%s.yml" % NEW)
    assert os.path.isfile(str(path)) and not os.path.islink(str(path)), "生活设定文件没生成"


def test_清单3_勾life新建后_GET_features_life为真(adapter, telegram, bun, life_dir):
    create_ok(adapter, features=["life"])
    body = adapter.ok(adapter.call("GET", "%s/%s/features" % (au.BOTS, NEW)))
    assert body["features"]["life"] is True, body["features"]


def test_清单4_不勾life新建_life为假_跳过项里没有life_生活设定文件照样生成(adapter, telegram, bun, life_dir):
    got = create_ok(adapter, features=["stickers"])
    assert got["features"]["life"] is False, got["features"]
    assert got["features"]["stickers"] is True, got["features"]
    assert [s["key"] for s in got["features_skipped"]] == [], got["features_skipped"]
    assert new_cfg(adapter)["features"]["life"] is False
    assert os.path.isfile(str(life_dir / ("%s.yml" % NEW))), "不勾 life 也应生成生活设定文件"


def test_清单5_生活设定目录是相对路径_勾life_life为假_跳过项是life_config_missing_配置无life_config(
        adapter, telegram, bun, monkeypatch):
    monkeypatch.setenv("DSH_CONSOLE_LIFE_DIR", "relative/life-dir")
    got = create_ok(adapter, features=["life"])
    assert got["features"]["life"] is False, got["features"]
    skipped = got["features_skipped"]
    assert [s["key"] for s in skipped] == ["life"], skipped
    assert [m["code"] for m in skipped[0]["missing"]] == ["life_config_missing"], skipped
    assert isinstance(skipped[0]["missing"][0]["message"], str) and skipped[0]["missing"][0]["message"]
    cfg = new_cfg(adapter)
    assert "life_config" not in cfg, "目录不合格时不该写 life_config"
    assert cfg["features"]["life"] is False
    assert not os.path.lexists(os.path.join("relative", "life-dir", "%s.yml" % NEW)), "不该在当前目录下生成文件"


def test_清单6_勾life又勾voice不给音色_voice为假且列入跳过_life为真(adapter, telegram, bun, life_dir):
    got = create_ok(adapter, features=["life", "voice"])
    assert got["features"]["voice"] is False and got["features"]["life"] is True, got["features"]
    assert [s["key"] for s in got["features_skipped"]] == ["voice"], got["features_skipped"]
    cfg = new_cfg(adapter)
    assert cfg["features"]["voice"] is False and cfg["features"]["life"] is True, cfg["features"]
