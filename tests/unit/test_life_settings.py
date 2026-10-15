# -*- coding: utf-8 -*-
"""生活设定（moments/life_settings.py）里验收测试碰不到的几处：一次改几个键、写法特殊、锁内发现被改过、
自检兜底、允许目录的禁区、只读字段原样交回。

直接调 view / save；bot 条目照适配服务 _own_bots 的形状手摆，目录全在 tmp_path。
"""
import contextlib
import os
from types import SimpleNamespace

import pytest
import yaml

from moments import life_settings as life


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, dsh_home = tmp_path / "home", tmp_path / "dsh-home"
    (home / ".claude").mkdir(parents=True)
    (dsh_home / "life").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DSH_BOT_HOME", str(dsh_home))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh_home / "configs"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs-legacy"))
    monkeypatch.delenv("DSH_CONSOLE_LIFE_DIR", raising=False)
    return SimpleNamespace(home=home, dsh_home=dsh_home, dir=dsh_home / "life")


def put_bot(env, text, own=None):
    """允许目录里放一份生活设定文件，返回 (bot, 名单, 文件)。"""
    path = env.dir / "botalpha.yml"
    path.write_bytes(text.encode("utf-8"))
    bot = {"id": "botalpha", "cfg": {"id": "botalpha", "life_config": str(path), **(own or {})}}
    return bot, [bot], path


def untouched(env):
    return not (env.dsh_home / "locks").exists() and not (env.dsh_home / "backups").exists()


def test_一次改几个键_替换去掉追加各自只动自己那一段_段外逐字不变(env):
    original = ("# 头注释\n"
                "city: 杭州  # 行尾注释\n"
                "\n"
                "# 夹在中间的注释\n"
                "user_address: 小测\n"
                "bio: 字段表外\n"
                "interest_keywords:\n"
                "  - 天文\n"
                "# 末尾注释\n")
    bot, bots, path = put_bot(env, original)
    message = life.save(bot, bots, {"interest_keywords": ["猫", "狗"], "city": "",
                                    "sleep_hours": ["23:00-07:00"], "user_address": "阿测"})
    assert message == life.SAVED
    assert path.read_text(encoding="utf-8") == ("# 头注释\n"
                                                "\n"
                                                "# 夹在中间的注释\n"
                                                "user_address: 阿测\n"
                                                "bio: 字段表外\n"
                                                "interest_keywords:\n"
                                                "  - 猫\n"
                                                "  - 狗\n"
                                                "# 末尾注释\n"
                                                "sleep_hours:\n"
                                                "  - 23:00-07:00\n")
    backups = os.listdir(env.dsh_home / "backups" / "life" / "botalpha")
    assert len(backups) == 1
    assert (env.dsh_home / "backups" / "life" / "botalpha" / backups[0]).read_text(encoding="utf-8") == original


def test_写法特殊的文件不改_回500_文件不动也不碰锁和备份(env):
    for text in ("city: 杭州\r\nuser_address: 小测\r\n",       # 有回车符
                 "{city: 杭州, user_address: 小测}\n",          # 顶层流风格
                 "city: 杭州\ncity: 南京\n"):                    # 要改的键出现两次
        bot, bots, path = put_bot(env, text)
        with pytest.raises(life.LifeError) as e:
            life.save(bot, bots, {"city": "上海"})
        assert (e.value.status, e.value.message) == (500, life.ODD_SYNTAX), text
        assert path.read_bytes() == text.encode("utf-8")
    assert untouched(env)


def test_读完之后拿锁之前别处改了文件_回409_不备份不写(env, monkeypatch):
    bot, bots, path = put_bot(env, "city: 杭州\n")
    real_lock = life._write_lock

    @contextlib.contextmanager
    def someone_wrote_first():
        path.write_text("city: 南京\n", encoding="utf-8")       # 另一个进程抢先写完放了锁
        with real_lock():
            yield

    monkeypatch.setattr(life, "_write_lock", someone_wrote_first)
    with pytest.raises(life.LifeError) as e:
        life.save(bot, bots, {"city": "上海"})
    assert (e.value.status, e.value.code, e.value.message) == (409, "busy", life.STALE)
    assert path.read_text(encoding="utf-8") == "city: 南京\n"
    assert not (env.dsh_home / "backups").exists()


def test_结尾两个换行的多行文字_改用双引号写_能存进去_后面的键不受影响(env):
    text = "persona_summary: 原来的人设\n\ncity: 杭州\n"
    bot, bots, path = put_bot(env, text)
    assert life.save(bot, bots, {"persona_summary": "结尾有两个换行\n\n"}) == life.SAVED
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc == {"persona_summary": "结尾有两个换行\n\n", "city": "杭州"}


def test_备份轮换_不带序号的那份排在同一秒带序号的前面():
    names = ["20261015-084153-01.yml", "20261015-084153.yml", "20261015-084152-02.yml"]
    assert sorted(names, key=life._backup_order) == [
        "20261015-084152-02.yml", "20261015-084153.yml", "20261015-084153-01.yml"]


def test_允许目录_缺省是本系统家目录下的life_落在禁区或家目录别处的一律不算(env, monkeypatch, tmp_path):
    assert life.allowed_dir() == (str(env.dir), os.path.realpath(env.dir))
    (env.dsh_home / "bots").mkdir()
    (env.home / ".claude" / "x").mkdir()
    disguised = tmp_path / "looks-fine"
    disguised.symlink_to(env.home / ".claude" / "x")           # 写法看着没事，真实路径在 ~/.claude 里
    for raw in (str(env.dsh_home), str(env.dsh_home / "bots"), str(env.home), "/", str(disguised),
                "~/.claude/x", str(tmp_path / "no-such-dir"), "relative/life"):
        monkeypatch.setenv("DSH_CONSOLE_LIFE_DIR", raw)
        assert life.allowed_dir() is None, raw


def test_只读字段原样交回不算错_改了才按只读拒(env):
    bot, bots, path = put_bot(env, "city: 杭州\nuser_address: 小测\n", own={"city": "上海"})
    got = life.view(bot, bots)
    assert got["values"]["city"] == "上海"
    assert life.save(bot, bots, got["values"]) == life.NO_CHANGE
    with pytest.raises(life.LifeError) as e:
        life.save(bot, bots, {"city": "苏州", "user_address": "阿测"})
    assert (e.value.status, e.value.fields) == (422, {"city": life.RO_OWN_YML})
    assert path.read_text(encoding="utf-8") == "city: 杭州\nuser_address: 小测\n"


def test_备份_同一秒轮换删掉较早的之后_新的一份仍接在最后_不拿回删掉的名字(monkeypatch, tmp_path):
    """6.5 与适配服务 10.2：同一秒连存 7 次、只留 2 份，留下的应是最近 2 份（人设备份：persona 目录、不带后缀）。

    从头找第一个空名字的话，轮换删掉这一秒的第一份之后，第 7 份会拿回那个名字，排到最前、随即被删。
    """
    import datetime
    import re
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path))
    frozen = datetime.datetime(2026, 10, 15, 9, 30, 0)
    monkeypatch.setattr(life, "datetime", SimpleNamespace(datetime=SimpleNamespace(now=lambda: frozen)))
    name_re = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]{2})?\Z")
    for i in range(7):
        folder = life._backup("botalpha", b"v%d" % i, kind="persona", suffix="")
        life._rotate(folder, name_re)
    assert os.path.realpath(folder) == os.path.realpath(str(tmp_path / "backups" / "persona" / "botalpha"))
    names = sorted(os.listdir(folder))
    assert names == ["20261015-093000-%02d" % i for i in range(5, 7)]
    contents = []
    for name in names:
        with open(os.path.join(folder, name), "rb") as f:
            contents.append(f.read())
    assert contents == [b"v%d" % i for i in range(5, 7)]
    assert all(os.stat(os.path.join(folder, n)).st_mode & 0o777 == 0o600 for n in names)
