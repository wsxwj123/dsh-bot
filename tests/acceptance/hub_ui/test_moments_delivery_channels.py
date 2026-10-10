# -*- coding: utf-8 -*-
"""用户在主页发朋友圈后，通知投给各 bot 的通道选择。守用户感觉得到的结果。

混跑期一个 bot 可能在新旧两套配置里各有一条记录，投递口径是"每个 bot 取在跑的那
一份"。

1. 跑在旧系统的 bot，通知写进旧系统那份配置的通道目录（chats/<id>/inbox），
   里面带着用户发的正文；
2. 跑在新系统的 bot，通知走它自己的网关通道（state 里有 api.key 与 api.port），
   不写 inbox，写进去没人读；
3. 停用的 bot 一边都不收；
4. 不许把通知投到没在跑的那套上。新系统那份 enabled:false 时，它自己的通道目录里
   一个通知文件都不许出现；
5. 同一个朋友圈名（life 名）撞车时只投一份，不许因为两条配置变成两条通知。

网关用本机假件（stub_gateway）扮演，只在这一层用替身。管理台到网关是 HTTP，
假件按 /v1/inject 的形状回应，管理台的投递决策都是真的。所有目录都在 tmp。

每条 bot 配置都显式写 dispatcher_port，指向本机的 dispatcher 假件或一个没人听
的端口。不写时拉起那一跳会落到注册表的派生端口，本机 17801 起就是生产
dispatcher 在听，测试的 POST /ensure_worker 会真发到生产身上。写了端口，这一跳
只打到测试自己的东西上，打到哪也还能断言。
"""
import json
from pathlib import Path

from .conftest import free_port, write_bot_cfg
from .stub_gateway import StubGateway

CHAT_ID = "77"


def _inbox_files(channel, chat_id=CHAT_ID):
    """该通道目录里用户朋友圈通知的文件列表。"""
    inbox = Path(channel) / "chats" / chat_id / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(inbox.glob("user-moment-*.json"))


def _make_dsh_bot(sandbox, name, *, dispatcher_port, life=None, enabled=True, gateway=None):
    """造一个新系统 bot，返回它的通道目录。

    gateway 传 StubGateway 时写 state/api.key 与 api.port，网关这才算"在跑"。
    dispatcher_port 必填，见文件头（不给默认值，漏传直接报错，别静默走派生端口）。
    """
    botdir = sandbox["tmp"] / "run" / ("dsh-%s" % name)
    channel = botdir / "channel"
    fields = dict(display_name=name, bot_channel_path=channel, chat_id=CHAT_ID,
                  dispatcher_port=dispatcher_port)
    if life is not None:
        fields["life_config"] = life
    if not enabled:
        fields["enabled"] = "false"
    write_bot_cfg(sandbox["env"]["HUB_CONFIGS_DSH_DIR"], name, **fields)
    (channel / "chats" / CHAT_ID).mkdir(parents=True, exist_ok=True)
    if gateway is not None:
        state = botdir / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "api.key").write_text(gateway.key + "\n", encoding="utf-8")
        (state / "api.port").write_text(str(gateway.port) + "\n", encoding="utf-8")
    return channel


def _make_legacy_bot(sandbox, name, *, dispatcher_port, enabled=True, display_name=None):
    """造一个旧系统 bot，返回它的通道目录。dispatcher_port 必填，见文件头。"""
    channel = sandbox["tmp"] / "run" / ("legacy-%s" % name)
    fields = dict(display_name=display_name or name, bot_channel_path=channel, chat_id=CHAT_ID,
                  dispatcher_port=dispatcher_port)
    if not enabled:
        fields["enabled"] = "false"
    write_bot_cfg(sandbox["env"]["HUB_CONFIGS_DIR"], name, **fields)
    return channel


def _post_moment(hub, text="今天下雨"):
    r = hub.post("/api/moment", json={"text": text})
    assert r.status_code == 200, "发朋友圈期望 200，实得 %s，%s" % (r.status_code, r.get_data(as_text=True))
    return r


def test_旧系统的bot通知写进它自己的通道目录(sandbox, hub, dispatcher):
    legacy_channel = _make_legacy_bot(sandbox, "bot2", display_name="李彤彤",
                                      dispatcher_port=dispatcher.port)
    # 新系统里也有一条 bot2，写着 enabled:false（意思是跑在旧系统）
    dsh_channel = _make_dsh_bot(sandbox, "bot2", enabled=False, dispatcher_port=free_port())

    _post_moment(hub, "今天下雨")

    files = _inbox_files(legacy_channel)
    assert len(files) == 1, "旧系统在跑的 bot2 应恰好收到一份通知，实得 %s" % files
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert "今天下雨" in body["text"], "通知里应带上用户发的正文，实得 %r" % body["text"][:200]
    assert _inbox_files(dsh_channel) == [], (
        "bot2 跑在旧系统，通知不许写进新系统那份的通道目录")


def test_新系统的bot通知走网关不写inbox(sandbox, hub, dispatcher):
    life = sandbox["tmp"] / "life"
    write_bot_cfg(life, "chenlulu", display_name="陈露露")

    gw = StubGateway(key="test-api-key-0123456789")
    gw.on("/v1/inject", 200, {"ok": True})
    gw.start()
    try:
        channel = _make_dsh_bot(sandbox, "bot4", life=life / "chenlulu.yml", gateway=gw,
                                dispatcher_port=dispatcher.port)

        _post_moment(hub, "在吗")

        hits = gw.hits("/v1/inject")
        assert len(hits) == 1, "网关应恰好收到一条投递，实得 %s 条" % len(hits)
        assert hits[0]["auth_ok"], "投递没带对网关口令"
        assert hits[0]["body"]["chat_id"] == CHAT_ID
        assert hits[0]["body"]["key"].startswith("user-moment:"), (
            "投递的幂等键不对，实得 %r" % hits[0]["body"]["key"])
        assert _inbox_files(channel) == [], "新系统的 bot 投给网关后不该再写 inbox"
    finally:
        gw.stop()


def test_两边都停用的bot一边也不收(sandbox, hub, dispatcher):
    legacy_channel = _make_legacy_bot(sandbox, "bot9", enabled=False,
                                      dispatcher_port=dispatcher.port)
    dsh_channel = _make_dsh_bot(sandbox, "bot9", enabled=False,
                                dispatcher_port=dispatcher.port)

    _post_moment(hub, "有人在吗")

    assert _inbox_files(legacy_channel) == [], "停用的 bot 不许收到通知"
    assert _inbox_files(dsh_channel) == [], "停用的 bot 不许收到通知（新系统这边）"


def test_新系统bot网关不在时也写自己目录_不写旧系统的(sandbox, hub, dispatcher):
    """新系统那份在跑、网关暂时没起来时，投递落回新系统自己的通道目录。
    旧系统那份是停用的，通知无论如何不许落过去。"""
    legacy_channel = _make_legacy_bot(sandbox, "bot4", enabled=False,
                                      dispatcher_port=free_port())
    dsh_channel = _make_dsh_bot(sandbox, "bot4", gateway=None,
                                dispatcher_port=dispatcher.port)

    _post_moment(hub, "早")

    assert len(_inbox_files(dsh_channel)) == 1, (
        "网关不在时通知应落回新系统自己的通道目录，实得 %s" % _inbox_files(dsh_channel))
    assert _inbox_files(legacy_channel) == [], (
        "bot4 的新系统那份在跑，通知不许投到没在跑的旧系统那份")


def test_同一个朋友圈名只投一份通知(sandbox, hub, dispatcher):
    """新栈 bot5（停用，life 指向旧栈 yasuna）与旧栈 yasuna 撞同一个朋友圈名。
    两个人格跑同一份生活数据，通知只该投一份，投到在跑的那份去。"""
    life = sandbox["tmp"] / "life"
    write_bot_cfg(life, "yasuna", display_name="淑仪")

    legacy_channel = _make_legacy_bot(sandbox, "yasuna", display_name="淑仪",
                                      dispatcher_port=dispatcher.port)
    dsh_channel = _make_dsh_bot(sandbox, "bot5", life=life / "yasuna.yml", enabled=False,
                                dispatcher_port=free_port())

    _post_moment(hub, "晚安")

    files = _inbox_files(legacy_channel)
    assert len(files) == 1, "撞名时应只投一份到在跑的那份，实得 %s" % files
    assert _inbox_files(dsh_channel) == [], (
        "bot5 跑在旧系统，通知不许写进新系统那份的通道目录，也不许重投一份")


def test_拉起worker打的是在跑那份配置写的端口(sandbox, hub, dispatcher):
    """混跑期新旧同名，旧栈那份在跑。拉起 worker 的端口必须取「在跑的那份」
    配置里写的值。

    按名字查全局注册表会查到新栈那份的端口（真机实测旧栈 17802 在听、新栈
    17952 拒连），通知写进了正确的 inbox，拉起那一跳却静默失败。这里新栈那份
    写一个没人听的端口，产品打错地方就一条都收不到，dispatcher 假件这边必须
    恰好收到一条。
    """
    dead_port = free_port()
    _make_legacy_bot(sandbox, "bot2", display_name="李彤彤",
                     dispatcher_port=dispatcher.port)
    _make_dsh_bot(sandbox, "bot2", enabled=False, dispatcher_port=dead_port)

    _post_moment(hub, "今天下雨")

    hits = dispatcher.hits("/ensure_worker")
    assert len(hits) == 1, (
        "拉起 worker 应恰好打到在跑那份配置的端口一次，实得 %s 条。打到新栈那份的"
        "端口（%s，没人听）或注册表派生端口都算红" % (len(hits), dead_port))
    assert hits[0]["method"] == "POST", "拉起用的是 POST，实得 %s" % hits[0]["method"]
