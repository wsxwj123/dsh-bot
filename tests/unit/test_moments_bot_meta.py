# -*- coding: utf-8 -*-
"""feed 页的 bot meta 映射：两个配置撞同一个朋友圈名时挑一份，且不静默。

真实场景：新栈 bot5（enabled: false，life_config 指旧栈 yasuna.yml）与旧栈
yasuna 的 ``_moments_id`` 都是 yasuna。原来的字典推导后者覆盖前者，静默少一个
chip，且将来两个不同的人撞同一个 life 名会真丢人。
"""
import moments.web as web


def _b(_bot_id, life=None, source="dsh", enabled=None):
    cfg = {"_bot_id": _bot_id, "id": _bot_id, "_source": source, "display_name": _bot_id}
    if life is not None:
        cfg["_life_id"] = life
    if enabled is not None:
        cfg["enabled"] = enabled
    return cfg


def _patch_meta(monkeypatch):
    monkeypatch.setattr(web, "_bot_meta",
                        lambda b: {"id": web._moments_id(b), "name": b["_bot_id"]})


def test_名字撞车时在跑的那份赢(monkeypatch, capsys):
    _patch_meta(monkeypatch)
    bots = [_b("bot5", life="yasuna", source="dsh", enabled=False),
            _b("yasuna", source="legacy")]

    m = web._moments_bot_meta(bots)

    assert list(m) == ["yasuna"]
    assert m["yasuna"]["name"] == "yasuna", "停了的那份不该顶掉在跑的那份"
    assert "撞车" in capsys.readouterr().err, "撞车必须在 stderr 留一行"


def test_两边都在跑时新系统优先(monkeypatch):
    _patch_meta(monkeypatch)
    bots = [_b("bot4", life="chenlulu", source="dsh"),
            _b("chenlulu", source="legacy")]

    m = web._moments_bot_meta(bots)

    assert m["chenlulu"]["name"] == "bot4"


def test_在跑的那份按文件名排在后面也不被覆盖(monkeypatch):
    """真实配置里 bot5 排在 yasuna 前面，旧实现是后到的 yasuna 覆盖 bot5，
    反过来在跑的那份后到时不能被前面的停用份顶掉。"""
    _patch_meta(monkeypatch)
    bots = [_b("yasuna", source="legacy"),
            _b("bot5", life="yasuna", source="dsh", enabled=False)]

    m = web._moments_bot_meta(bots)

    assert m["yasuna"]["name"] == "yasuna"


def test_名字不撞时一个都不少也不写stderr(monkeypatch, capsys):
    _patch_meta(monkeypatch)
    bots = [_b("bot2", source="legacy"), _b("bot4", life="chenlulu")]

    m = web._moments_bot_meta(bots)

    assert sorted(m) == ["bot2", "chenlulu"]
    assert capsys.readouterr().err == ""
