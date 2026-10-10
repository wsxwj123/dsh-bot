# -*- coding: utf-8 -*-
"""投递路径里的名字口径。

混跑期同一个 bot 有两个名字，用途不同：
- 配置文件名（``_bot_id``，如 bot4）：端口注册表、启停判定按它建键；
- 朋友圈里的 life 名（``_moments_id``，如 chenlulu）：评论署名按它记。

用户发朋友圈后的通知原本拿 life 名去调 ``_ensure_worker_alive``，新系统的 bot
（bot4 的 life 是 chenlulu）查不到端口，通知写进 inbox 却没人被拉起。
"""
import os


def _cfg(tmp_path, **over):
    cfg = {"_bot_id": "bot4", "_life_id": "chenlulu", "id": "bot4",
           "bot_channel_path": str(tmp_path), "chat_id": "123", "display_name": "陈露露"}
    cfg.update(over)
    return cfg


def _stub(monkeypatch, web, spawned):
    monkeypatch.setattr(web, "_ensure_worker_alive",
                        lambda bid, cid, d, cfg=None: spawned.append(bid))
    monkeypatch.setattr(web, "_deliver_dsh", lambda *a, **k: False)
    monkeypatch.setattr(web, "_user_display_name", lambda: "我")
    monkeypatch.setattr(web.db, "list_comments", lambda mid: [])
    monkeypatch.setattr(web.db, "list_moments", lambda **k: [])


def test_拉起worker用配置文件名而不是life名(monkeypatch, tmp_path):
    import moments.web as web
    spawned = []
    _stub(monkeypatch, web, spawned)

    web._trigger_bot_see_user_moment(_cfg(tmp_path), 1, "你好", None, "public")

    assert spawned == ["bot4"]


def test_旧系统的bot没有life名时照样拉起(monkeypatch, tmp_path):
    import moments.web as web
    spawned = []
    _stub(monkeypatch, web, spawned)
    cfg = _cfg(tmp_path, _bot_id="bot2", id="bot2")
    cfg.pop("_life_id")

    web._trigger_bot_see_user_moment(cfg, 2, "在吗", None, "public")

    assert spawned == ["bot2"]


def test_通知文件仍写进该bot的inbox(monkeypatch, tmp_path):
    import moments.web as web
    _stub(monkeypatch, web, [])

    web._trigger_bot_see_user_moment(_cfg(tmp_path), 3, "早", None, "public")

    inbox = os.path.join(str(tmp_path), "chats", "123", "inbox")
    files = [f for f in os.listdir(inbox) if f.startswith("user-moment-")]
    assert len(files) == 1, files
