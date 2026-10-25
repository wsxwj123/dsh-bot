# -*- coding: utf-8 -*-
"""create-life-file 验收：dsh 新建 bot 时生成最小生活设定文件。

依据只有 .devflow/create-life-file-接口.md（行为清单 1 到 13 条，编号对应用例名里的“清单N”）。
不读实现。夹具与调用写法照抄 test_adapter_v10_create.py、test_adapter_v04_life.py、
test_create_bot.py。生活设定目录经 DSH_CONSOLE_LIFE_DIR 指到沙箱 DSH_BOT_HOME 下的 life（默认不预先建出来）。
断言信息里不放 env 与请求体整体，只取单个键。
"""
import os

import pytest
import yaml

from . import adapter_util as au
from . import botops_util as u
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

NEW = "botnew"


def good_body(**over):
    body = {"bot_id": NEW, "telegram_token": au.NEW_TG_TOKEN, "chat_id": au.NEW_CHAT}
    body.update(over)
    return body


def life_url(bot_id):
    return "%s/%s/life" % (au.BOTS, bot_id)


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


def assert_life_file_ok(path):
    assert os.path.lexists(str(path)), "生活设定文件没生成"
    assert not os.path.islink(str(path)), "生活设定文件不该是软链"
    assert os.path.isfile(str(path)), "生活设定文件应是普通文件"
    assert au.mode_of(path) == 0o600, "生活设定文件权限应为 0600，实际 %o" % au.mode_of(path)


def assert_nothing_left(adapter):
    sb = adapter.sandbox
    assert not os.path.lexists(str(sb.dsh_cfg / ("%s.yml" % NEW))), "不该留下 bot 配置 yml"
    assert not os.path.lexists(str(sb.dsh_home / "bots" / NEW)), "不该留下 bot 目录"


# ---------------------------------------------------------------- 清单 1、2、3

def test_清单1_适配器新建成功_生活设定文件是0600普通文件(adapter, telegram, bun, life_dir):
    create_ok(adapter)
    assert_life_file_ok(life_dir / ("%s.yml" % NEW))


def test_清单2_生活设定目录事先不存在_新建后目录是0700_文件是0600(adapter, telegram, bun, life_dir):
    create_ok(adapter)
    assert os.path.isdir(str(life_dir)), "生活设定目录应被建出来"
    assert au.mode_of(life_dir) == 0o700, "生活设定目录权限应为 0700，实际 %o" % au.mode_of(life_dir)
    assert_life_file_ok(life_dir / ("%s.yml" % NEW))


def test_清单3_新建成功_配置顶层life_config等于生活设定文件绝对路径(adapter, telegram, bun, life_dir):
    create_ok(adapter)
    value = new_cfg(adapter).get("life_config")
    assert isinstance(value, str) and os.path.isabs(value), "life_config 应是绝对路径字符串"
    want = life_dir / ("%s.yml" % NEW)
    assert os.path.realpath(value) == os.path.realpath(str(want)), "life_config 没指向 <生活设定目录>/<bot_id>.yml"
    assert os.path.basename(value) == "%s.yml" % NEW


# ---------------------------------------------------------------- 清单 4、5

def test_清单4_新建后GET_life_回200且ok为真(adapter, telegram, bun, life_dir):
    create_ok(adapter)
    resp = adapter.call("GET", life_url(NEW))
    body = adapter.ok(resp)
    assert resp.status_code == 200 and body["ok"] is True


def test_清单5_新建后PUT_sleep_hours_applied为真_再GET读回同值(adapter, telegram, bun, life_dir):
    create_ok(adapter)
    put = adapter.ok(adapter.call("PUT", life_url(NEW), {"values": {"sleep_hours": ["00:00-07:30"]}}))
    assert put.get("applied") is True
    got = adapter.ok(adapter.call("GET", life_url(NEW)))
    assert got["values"]["sleep_hours"] == ["00:00-07:30"]


# ---------------------------------------------------------------- 清单 6、7

def test_清单6_新建带person且与编号不同_GET_bots里person是给定值(adapter, telegram, bun, life_dir):
    got = create_ok(adapter, person="lucy")
    assert got["person"] == "lucy"
    assert au.rows_by_id(adapter)[NEW]["person"] == "lucy"


def test_清单7_新建不带person_person等于编号(adapter, telegram, bun, life_dir):
    got = create_ok(adapter)
    assert got["person"] == NEW
    assert au.rows_by_id(adapter)[NEW]["person"] == NEW


# ---------------------------------------------------------------- 清单 8、9

def test_清单8_生活设定目录已有同名文件_409_exists_不留配置不留目录_原文件不变(adapter, telegram, bun, life_dir):
    existing = au.put_file(life_dir / ("%s.yml" % NEW), "原有内容：不许被覆盖\nkeep: 1\n")
    before = au.file_mark(existing)
    resp = adapter.call("POST", au.BOTS, good_body())
    body = adapter.expect_error(resp, 409, "exists")
    assert "生活设定" in body["message"], "message 应含“生活设定”字样"
    assert_nothing_left(adapter)
    assert au.file_mark(existing) == before, "原有生活设定文件被动了"
    assert existing.read_bytes() == "原有内容：不许被覆盖\nkeep: 1\n".encode("utf-8")


def test_清单9_生活设定目录里同名是软链_409_exists_软链本身与目标都不变(adapter, telegram, bun, life_dir):
    target = au.put_file(adapter.sandbox.tmp / "elsewhere" / "real.yml", "别处的文件\n")
    life_dir.mkdir(mode=0o700)
    link = life_dir / ("%s.yml" % NEW)
    os.symlink(str(target), str(link))
    link_before, target_before = au.file_mark(link), au.file_mark(target)
    resp = adapter.call("POST", au.BOTS, good_body())
    body = adapter.expect_error(resp, 409, "exists")
    assert "生活设定" in body["message"]
    assert_nothing_left(adapter)
    assert os.path.islink(str(link)) and os.readlink(str(link)) == str(target), "软链被改了"
    assert au.file_mark(link) == link_before
    assert au.file_mark(target) == target_before, "软链指向的文件被动了"


# ---------------------------------------------------------------- 清单 10、11

def test_清单10_装启动项失败而撤回_生活设定文件不在原位(adapter, telegram, bun, life_dir, monkeypatch):
    au.use_trash(adapter.sandbox, monkeypatch)
    bun.expect(exit=0).expect(exit=3)       # install 成了，install-jobs 失败（同 v10 用例）
    adapter.expect_error(adapter.call("POST", au.BOTS, good_body()), 503, "unavailable")
    assert not os.path.lexists(str(life_dir / ("%s.yml" % NEW))), "撤回后生活设定文件还留在原位"
    assert_nothing_left(adapter)


def test_清单11_删除新建的bot_生活设定文件不在原位(adapter, telegram, bun, life_dir, monkeypatch):
    au.use_trash(adapter.sandbox, monkeypatch)
    create_ok(adapter)
    path = life_dir / ("%s.yml" % NEW)
    assert os.path.lexists(str(path)), "前提不成立：新建后生活设定文件应已存在"
    adapter.ok(adapter.call("POST", au.delete_url(NEW), {"confirm": NEW}))
    assert not os.path.lexists(str(path)), "删除后生活设定文件还留在原位"


# ---------------------------------------------------------------- 清单 12

def test_清单12_本仓新建页新建成功_生活设定文件在_配置有life_config(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin, monkeypatch):
    life_dir_hub = sandbox.dsh_home / "life"
    monkeypatch.setenv("DSH_CONSOLE_LIFE_DIR", str(life_dir_hub))
    telegram.mode = "ok"
    bun.expect(exit=0).expect(exit=0)
    launchctl.set_default(installed=True, running=True, pid=4242)
    heartbeat.tick("xiaoyu", pid=4242)
    r = client.post("/hub/api/bots", json={
        "bot_id": "xiaoyu", "telegram_token": u.FAKE_TOKEN, "dispatcher_port": u.free_port(),
        "chat_id": 123456789, "display_name": "小雨", "persona_text": "我是小雨的人设正文。"})
    assert r.status_code == 201, "新建页应回 201，实际 %s" % r.status_code
    path = life_dir_hub / "xiaoyu.yml"
    assert_life_file_ok(path)
    value = u.read_yaml(sandbox.dsh_cfg / "xiaoyu.yml").get("life_config")
    assert isinstance(value, str) and os.path.isabs(value)
    assert os.path.realpath(value) == os.path.realpath(str(path)), "life_config 没指向生活设定文件"


# ---------------------------------------------------------------- 清单 13

def test_清单13_已有bot带life_config_新建另一只后它的配置与生活设定文件字节不变(adapter, telegram, bun, life_dir):
    alpha = au.full_bot(adapter, "botalpha")      # 生活设定文件落在 <DSH_BOT_HOME>/life/botalpha.yml
    assert alpha.life.parent == life_dir
    yml_before, life_before = au.file_mark(alpha.yml), au.file_mark(alpha.life)
    yml_bytes, life_bytes = alpha.yml.read_bytes(), alpha.life.read_bytes()
    create_ok(adapter)
    assert alpha.yml.read_bytes() == yml_bytes, "已有 bot 的配置 yml 字节变了"
    assert alpha.life.read_bytes() == life_bytes, "已有 bot 的生活设定文件字节变了"
    assert au.file_mark(alpha.yml) == yml_before and au.file_mark(alpha.life) == life_before
