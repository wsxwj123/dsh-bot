# -*- coding: utf-8 -*-
"""「哪些 bot 该被投递、每个投给谁」的唯一来源（混跑期两套配置根）。

盯三件事，都是曾经的错法：

1. 「某个 bot 现在算不算在跑」只有一处判定（``bots_registry.running_ids``）：
   名字下**只要有一份启用配置就算在跑**。新系统那份 ``enabled: false`` 的意思是
   "跑在旧系统"，按"同名以新系统为准"判会把旧栈正在跑的 bot 判成停的。
   混跑期某个 bot 迁到新系统、旧系统留着一份停用配置时，通知会写进 inbox 但
   worker 不会被拉起，就是这条判错的后果。
2. 「同一个人的两份配置留哪一份」只有一处（``config_sources.pick_per_person``）：
   展示面（朋友圈页 chip）与投递面必须挑同一份，两份都启用时不许各投一遍。
3. 撞名留痕一个进程只写一行（管理台是 launchd 常驻服务，每次渲染都写会一直长）。

全部在 tmp 配置根里跑，不读真实 ``~/.dsh-bot``（HOME 钉在 tmp）。
"""
import sys
from pathlib import Path

import pytest

import bots_registry
from moments import config_sources as cs

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write(d, bot_id, **fields):
    d.mkdir(parents=True, exist_ok=True)
    text = "id: %s\n" % bot_id
    for k, v in sorted(fields.items()):
        if isinstance(v, bool):
            v = "true" if v else "false"
        text += "%s: %s\n" % (k, v)
    (d / ("%s.yml" % bot_id)).write_text(text, encoding="utf-8")


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """两套 tmp 配置根 + 钉住的 HOME（默认的 ~/.dsh-bot 绝不落到真家目录）。"""
    legacy = tmp_path / "legacy"
    dsh = tmp_path / "dsh"
    legacy.mkdir()
    dsh.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    life = tmp_path / "life"
    life.mkdir()
    _write(life, "yasuna", display_name="淑仪")     # 新系统 bot5 的 life_config 指向它
    return legacy, dsh


def _life(roots):
    """新系统配置里 life_config 指的路（``load_bot`` 会真读它，指向不存在的文件等于没这个 bot）。"""
    return Path(roots[0]).parent / "life" / "yasuna.yml"


def _dirs(roots):
    return [str(d) for d in roots]


# ------------------------------------------------------------ 在跑与否

def test_迁到新系统旧栈留着停用配置_仍算在跑(roots):
    """dsh 启用、旧根残留 enabled: false —— 跑在新系统，不是停用。"""
    _write(roots[0], "bot4", enabled=False)
    _write(roots[1], "bot4")
    assert bots_registry.running_ids(dirs=_dirs(roots)) == {"bot4"}
    assert bots_registry.disabled_ids(dirs=_dirs(roots)) == set()


def test_新系统写着跑在旧系统_旧根启用_也算在跑(roots):
    _write(roots[0], "bot2", enabled=True)
    _write(roots[1], "bot2", enabled=False)
    assert bots_registry.disabled_ids(dirs=_dirs(roots)) == set()


def test_两边都停用才算停(roots):
    _write(roots[0], "bot9", enabled=False)
    _write(roots[1], "bot9", enabled=False)
    assert bots_registry.running_ids(dirs=_dirs(roots)) == set()
    assert bots_registry.disabled_ids(dirs=_dirs(roots)) == {"bot9"}


def test_只有一边有配置时口径不变(roots):
    _write(roots[0], "bot2")
    _write(roots[1], "bot5", enabled=False)
    assert bots_registry.running_ids(dirs=_dirs(roots)) == {"bot2"}
    assert bots_registry.disabled_ids(dirs=_dirs(roots)) == {"bot5"}


def test_disabled_ids_safe跟投递面同一口径(roots):
    """迁到新系统、旧栈残留停用那份时，后台生产者不许把它当停用。"""
    _write(roots[0], "bot4", enabled=False)
    _write(roots[1], "bot4")
    assert bots_registry.disabled_ids_safe() == set()
    # 两边都停用的照旧判得出来
    _write(roots[0], "bot9", enabled=False)
    assert bots_registry.disabled_ids_safe() == {"bot9"}


# ------------------------------------------------------------ 投递名单

def test_投递名单一个人只留一份_两份都启用时新系统赢(roots):
    _write(roots[0], "yasuna", bot_channel_path="/old/yasuna")
    _write(roots[1], "bot5", life_config=_life(roots), bot_channel_path="/new/bot5")
    names = [(c["_bot_id"], c["bot_channel_path"]) for c in cs.delivery_configs()]
    assert names == [("bot5", "/new/bot5")], names
    # 展示面 chip 挑的是同一份：同一个人两张脸不许各指一个 bot
    picked, collisions = cs.pick_per_person(cs.panel_bot_configs())
    assert [c["_bot_id"] for c in picked] == ["bot5"]
    assert [c["key"] for c in collisions] == ["yasuna"]


def test_撞名里在跑的那份赢过停用那份(roots):
    _write(roots[0], "yasuna", bot_channel_path="/old/yasuna")
    _write(roots[1], "bot5", enabled=False, life_config=_life(roots),
           bot_channel_path="/new/bot5")
    assert [c["_bot_id"] for c in cs.delivery_configs()] == ["yasuna"]


def test_投递名单只含在跑的bot(roots):
    _write(roots[0], "bot2", bot_channel_path="/old/bot2")
    _write(roots[1], "bot2", enabled=False, bot_channel_path="/new/bot2")
    _write(roots[1], "bot9", enabled=False, bot_channel_path="/new/bot9")
    assert [(c["_bot_id"], c["bot_channel_path"]) for c in cs.delivery_configs()] == [
        ("bot2", "/old/bot2")]


def test_按朋友圈名反查拿到的是同一份(roots):
    _write(roots[0], "yasuna", bot_channel_path="/old/yasuna")
    _write(roots[1], "bot5", life_config=_life(roots), bot_channel_path="/new/bot5")
    assert cs.delivery_config_for("yasuna")["bot_channel_path"] == "/new/bot5"
    assert cs.delivery_config_for("chenlulu") is None


# ------------------------------------------------------------ 启停开关落点

def test_启停开关写正在跑的那份(roots):
    _write(roots[0], "bot2", bot_channel_path="/old/bot2")
    _write(roots[1], "bot2", enabled=False, bot_channel_path="/new/bot2")
    assert cs.find_path("bot2") == roots[0] / "bot2.yml", "旧栈在跑，停止要停旧那份"
    # 都在跑时新系统那份生效（口径与展示面、投递面同一处）
    _write(roots[1], "both")
    _write(roots[0], "both")
    assert cs.find_path("both") == roots[1] / "both.yml"
    # 一个都没在跑：落点还是优先级最高那份（启用它等于让它到新系统上跑）
    _write(roots[0], "dead", enabled=False)
    _write(roots[1], "dead", enabled=False)
    assert cs.find_path("dead") == roots[1] / "dead.yml"
    assert cs.effective_config("dead")["_bot_id"] == "dead"


# ------------------------------------------------------------ 撞名留痕不刷屏

def _cfg(bot_id, life=None, source="dsh", enabled=None):
    cfg = {"_bot_id": bot_id, "id": bot_id, "_source": source, "display_name": bot_id}
    if life is not None:
        cfg["_life_id"] = life
    if enabled is not None:
        cfg["enabled"] = enabled
    return cfg


def test_撞名留痕一个进程只写一行(capsys):
    import moments.web as web
    web.app.extensions.pop("moments_collision_warned", None)
    a = _cfg("bot5", life="yasuna", enabled=False)
    b = _cfg("yasuna", source="legacy")
    with web.app.app_context():
        for _ in range(5):
            web._warn_collision("yasuna", a, b)
    err = capsys.readouterr().err
    assert err.count("撞车") == 1, "同一个撞名组合只该提示一次，实得 %r" % err
    assert "yasuna" in err and "bot5" in err


def test_撞名组合变了要重新提示(capsys):
    import moments.web as web
    web.app.extensions.pop("moments_collision_warned", None)
    with web.app.app_context():
        web._warn_collision("yasuna", _cfg("bot5", life="yasuna", enabled=False),
                            _cfg("yasuna", source="legacy"))
        web._warn_collision("chenlulu", _cfg("bot4", life="chenlulu", enabled=False),
                            _cfg("chenlulu", source="legacy"))
    err = capsys.readouterr().err
    assert err.count("撞车") == 2, err


def test_没有app上下文时不去重(capsys):
    """直接调函数的场合（白盒单测）每次都写，别把信号藏起来。"""
    import moments.web as web
    for _ in range(2):
        web._warn_collision("yasuna", _cfg("bot5", life="yasuna", enabled=False),
                            _cfg("yasuna", source="legacy"))
    assert capsys.readouterr().err.count("撞车") == 2
