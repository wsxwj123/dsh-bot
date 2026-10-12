# -*- coding: utf-8 -*-
"""§4.2 B/C 段 + §4.3 响应形状与那 8 条必覆盖用例（新系统落点与 C 段口径）。

新建 bot 落新系统（INTERFACE §6/§7）：配置进 `HUB_CONFIGS_DSH_DIR`、频道树进
`<DSH_BOT_HOME>/bots/<bot>/channel`，旧根（`HUB_CONFIGS_DIR`、`HUB_CHANNELS_DIR`）
一字节都不产生。register 走假 bun（`HUB_BUN`，两条命令 `install` / `install-jobs`）；
restart 步等 §5 的存活条件（心跳 + `HUB_LAUNCHCTL` 的 print），该步自己等、不产生 job；
`HUB_RESTART_SCRIPT` 这条接缝已退役（§1 末行），测试不再拿它当开关。
"""
import json
import re
import shutil
import threading

import pytest

import hub2util as u
from hub2util import addbot_payload, steps_by_name

EP = "/hub/api/bots"
TOKEN = "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11"
STEPS = ["validate", "getme", "files", "register", "restart"]


def _create(api, port, **over):
    over.setdefault("telegram_token", TOKEN)
    over.setdefault("dispatcher_port", port)
    return api.post(EP, addbot_payload(**over))


@pytest.fixture
def ok_api(apif, telegram, fake_bun, fake_launchctl, alive_bot, tmp_path,
           dsh_home, dsh_configs_dir):
    """一切外部条件都摆好的客户端：getMe 通、假 bun 两条命令都成（装的是新根那份 yml）、
    假 launchd 报 running、心跳持续新鲜且 pid 与 print 一致（§5 五条齐）→ C 段五步能全 done。

    覆盖命令（三个重启触发口用的 `HUB_RESTART_CMD`）顺手写成「留痕」：谁真执行了它，
    `ok_api.restart_trace` 这个文件就会出现 —— 新建流程的 restart 步不该碰它（§6）。
    """
    telegram.mode = "ok"
    alive_bot("xiaoyu")
    trace = tmp_path / "restart-cmd-ran"
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base,
               HUB_BUN=str(fake_bun().path),
               HUB_LAUNCHCTL=str(fake_launchctl("running")),
               HUB_RESTART_CMD=f"/usr/bin/touch {trace}",
               DSH_BOT_HOME=str(dsh_home), HUB_CONFIGS_DSH_DIR=str(dsh_configs_dir))
    api.restart_trace = trace
    return api


@pytest.mark.slow
def test_加bot_全流程成功_201且五个步骤都done(ok_api, port, launchagents_dir):
    """§4.3 用例①。C 段的 restart 是「等 §5 判活」，自己等、不产生 job ——
    真起过 job 的话那条覆盖命令会留下痕迹。"""
    code, body = _create(ok_api, port)
    assert code == 201, body
    assert body["ok"] is True
    assert body["bot_id"] == "xiaoyu"
    assert body["username"] == "my_bot"
    steps = steps_by_name(body)
    assert [s["name"] for s in body["steps"]] == STEPS
    assert all(s["state"] == "done" for s in body["steps"]), body["steps"]
    assert "job_id" not in steps["restart"], "§6：restart 步自己等，不产生 job"
    assert not ok_api.restart_trace.exists(), "新建流程里跑了重启覆盖命令 = 偷偷起了 job"
    assert body["restart_hint"] == {"bot": False, "moments_web": False}, \
        "§6 S4：201 档五步全 done，没有待补动作"
    assert list(launchagents_dir.iterdir()) == [], "面板不该自己往 LaunchAgents 写 plist"


@pytest.mark.slow
def test_加bot_成功后产物清单齐全且内容正确(ok_api, port, fake_bun,
                                            dsh_configs_dir, dsh_home,
                                            configs_dir, channels_dir):
    code, body = _create(ok_api, port, chat_id=987654321,
                         display_name="小雨", persona_text="我是小雨的人设正文。")
    assert code == 201, body
    chan = dsh_home / "bots" / "xiaoyu" / "channel"
    assert (chan / ".env").read_text(encoding="utf-8") == \
        f"TELEGRAM_BOT_TOKEN={TOKEN}\n"
    assert json.loads((chan / "access.json").read_text(encoding="utf-8")) == \
        {"allowFrom": [987654321]}
    assert (chan / "CLAUDE.md").read_text(encoding="utf-8") == "我是小雨的人设正文。"
    assert (chan / "memory" / "MEMORY.md").exists()
    assert not (chan / "logs").exists(), "§7：频道树里不带 logs（dsh 的日志在 <bot 家>/logs）"
    yml_path = dsh_configs_dir / "xiaoyu.yml"
    yml = u.load(yml_path)
    assert yml["id"] == "xiaoyu"
    assert yml["display_name"] == "小雨"
    assert yml["chat_id"] == 987654321
    assert yml["dispatcher_port"] == port
    assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                        str(yml["namespace_uuid"]))
    assert "bot_channel_path" in yml
    assert TOKEN not in u.text(yml_path), "yml 里带出了 token"
    # register 装的就是新根那份 yml，两条命令各一次（§6）
    assert [c["argv"][1:] for c in fake_bun().calls] == \
        [["install", str(yml_path)], ["install-jobs", str(yml_path)]]
    # 旧根一字节都不产生：落点换了，不是多写一份
    assert list(configs_dir.iterdir()) == []
    assert list(channels_dir.iterdir()) == []


@pytest.mark.slow
def test_加bot_成功后每个产物的权限都对(ok_api, port, dsh_configs_dir, dsh_home):
    """§4.3 用例⑧ / F5：move 跨文件系统会走 copy 路径，权限必须复核。"""
    code, body = _create(ok_api, port)
    assert code == 201, body
    home = dsh_home / "bots" / "xiaoyu"
    chan = home / "channel"
    assert u.mode_of(home) == 0o700
    assert u.mode_of(chan) == 0o700
    assert u.mode_of(chan / "memory") == 0o700
    assert u.mode_of(chan / ".env") == 0o600
    assert u.mode_of(chan / "access.json") == 0o600
    assert u.mode_of(chan / "CLAUDE.md") == 0o600
    assert u.mode_of(chan / "memory" / "MEMORY.md") == 0o600
    assert u.mode_of(dsh_configs_dir / "xiaoyu.yml") == 0o600


@pytest.mark.slow
def test_加bot_token只落在env里_别处一个字都没有(ok_api, port, dsh_configs_dir, dsh_home,
                                                 configs_dir, channels_dir, capfd):
    """F5：token 的唯一落点是 `<bot 家>/channel/.env`（新旧两套根一起翻）。"""
    code, body = _create(ok_api, port)
    assert code == 201, body
    out, err = capfd.readouterr()
    assert TOKEN not in u.flat_text(body), "响应里带出了 token"
    assert TOKEN not in out and TOKEN not in err, "token 进了 stdout/stderr"
    hits = []
    for root in (dsh_home, dsh_configs_dir, configs_dir, channels_dir):
        for p in root.rglob("*"):
            if p.is_file() and TOKEN in p.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(p))
    assert hits == [str(dsh_home / "bots" / "xiaoyu" / "channel" / ".env")], hits
    assert TOKEN not in u.text(dsh_configs_dir / "xiaoyu.yml")


@pytest.mark.slow
def test_加bot_B段中途失败_500且磁盘干净(apif, telegram, port, monkeypatch, dsh_configs_dir,
                                         dsh_home, configs_dir, channels_dir):
    """§4.3 用例③。同时验「绝不 rm -rf 已存在目录」：邻居 bot 的目录必须毫发无伤。"""
    neighbor = dsh_home / "bots" / "chenlulu" / "channel"
    neighbor.mkdir(parents=True)
    (neighbor / "keepme.txt").write_text("别删我", encoding="utf-8")
    telegram.mode = "ok"
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base)

    def boom(*a, **kw):
        raise OSError("mocked move failure")

    monkeypatch.setattr(shutil, "move", boom)
    code, body = _create(api, port)
    assert code == 500, body
    assert body["error"] == "internal"
    assert " " not in body["detail"].strip(), "detail 只该给异常类名"
    assert not (dsh_configs_dir / "xiaoyu.yml").exists()
    assert not (dsh_home / "bots" / "xiaoyu").exists(), "空的 <bot 家> 该被收走"
    assert (neighbor / "keepme.txt").read_text(encoding="utf-8") == "别删我"
    assert list(configs_dir.iterdir()) == [] and list(channels_dir.iterdir()) == []


@pytest.mark.slow
def test_加bot_register失败_207且文件仍在且子进程原文不进响应(apif, telegram, fake_bun,
                                                        port, dsh_configs_dir, dsh_home):
    """§4.3 用例④：C 段失败不回滚 B 段 —— 文件已经是对的，用户不该白填一遍表单。
    register 走假 bun（§6）：`install` 非 0 即短路，`install-jobs` 不再跑；
    detail 只给机读码、manual_hint 只给固定命令文本，子进程输出一个字不进响应（S9）。"""
    telegram.mode = "ok"
    bun = fake_bun("install")
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base, HUB_BUN=str(bun.path))
    code, body = _create(api, port)
    assert code == 207, body
    assert body["error"] == "bot_created_partial"
    reg = steps_by_name(body)["register"]
    assert reg["state"] == "failed"
    assert reg["detail"] == "install_failed"
    assert [c["argv"][1] for c in bun.calls] == ["install"], "install 失败还接着跑 install-jobs"
    assert bun.calls[0]["argv"][2] == str(dsh_configs_dir / "xiaoyu.yml"), \
        "register 装的不是新根那份 yml"
    assert body["restart_hint"] == {"bot": True, "moments_web": False}, \
        "§6 S4：207 档待补的是「让 bot 跑起来」面"
    hint = body.get("manual_hint") or reg.get("manual_hint") or ""
    assert "autostart.ts" in hint, f"207 必须给确切的手工命令：{hint!r}"
    assert "fake bun 的原始输出" not in u.flat_text(body), "子进程原文进了响应（S9）"
    assert (dsh_configs_dir / "xiaoyu.yml").exists()
    assert (dsh_home / "bots" / "xiaoyu" / "channel" / ".env").exists()


@pytest.mark.slow
def test_加bot_重启接缝已退役_脚本在场也不被执行(apif, telegram, fake_bun, fake_launchctl,
                                            alive_bot, tmp_path, port, dsh_configs_dir):
    """§1 末行：`HUB_RESTART_SCRIPT` 退役，实现与测试都不再读它。
    接缝指到一份「执行必留痕」的旧脚本：旧口径下它会触发 207，现在成败只看 §5 判活，
    所以照样 201，而且那个文件根本没被跑过。"""
    trace = tmp_path / "old-script-ran"
    old = tmp_path / "restart-bots-old.sh"
    old.write_text(f"#!/bin/sh\n# 老用户的工作副本：硬编码 bot 列表\ntouch {trace}\n",
                   encoding="utf-8")
    old.chmod(0o755)
    telegram.mode = "ok"
    alive_bot("xiaoyu")
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base, HUB_BUN=str(fake_bun().path),
               HUB_LAUNCHCTL=str(fake_launchctl("running")),
               HUB_RESTART_SCRIPT=str(old))
    code, body = _create(api, port)
    assert code == 201, body
    assert steps_by_name(body)["restart"]["state"] == "done"
    assert not trace.exists(), "退役的接缝还被执行了"
    assert (dsh_configs_dir / "xiaoyu.yml").exists()


@pytest.mark.slow
def test_加bot_重启接缝已退役_文件不存在也不构成失败(apif, telegram, fake_bun, fake_launchctl,
                                              alive_bot, tmp_path, port, dsh_configs_dir):
    """旧口径「自检脚本文件不存在 = 自检不通过 → 207」已作废（§1 末行）：
    文件不在场照样 201、restart 是 done。"""
    telegram.mode = "ok"
    alive_bot("xiaoyu")
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base, HUB_BUN=str(fake_bun().path),
               HUB_LAUNCHCTL=str(fake_launchctl("running")),
               HUB_RESTART_SCRIPT=str(tmp_path / "gone" / "restart-bots.sh"))
    code, body = _create(api, port)
    assert code == 201, body
    assert steps_by_name(body)["restart"]["state"] == "done"
    assert (dsh_configs_dir / "xiaoyu.yml").exists()


@pytest.mark.slow
def test_加bot_两条命令都成功但bot没在跑_207且restart是failed(apif, telegram, fake_bun,
                                                       fake_launchctl, port, dsh_configs_dir):
    """§4.3 用例⑥ / F4：成功判定改看 §5 判活（心跳 + launchd 状态），不看命令退出码。
    这里假 bun 两条命令都退 0（子进程「成功」）、launchd 查无此服务 —— 必须是 failed，
    不是 done：退出码 0 就记 done = 成功响应撒谎。"""
    telegram.mode = "ok"
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base, HUB_BUN=str(fake_bun().path),
               HUB_LAUNCHCTL=str(fake_launchctl("stopped")))
    code, body = _create(api, port)
    assert code == 207, body
    st = steps_by_name(body)["restart"]
    assert st["state"] == "failed", "命令退出码 0 就记 done = 成功响应撒谎"
    assert body["restart_hint"] == {"bot": True, "moments_web": False}
    assert (dsh_configs_dir / "xiaoyu.yml").exists()


@pytest.mark.slow
def test_加bot_重复加同名bot_第二次409_bot_exists且不覆盖(ok_api, port, port2,
                                                     dsh_configs_dir, dsh_home):
    """幂等反面：同一个表单点两次，第二次必须被拒且不动第一次的产物。"""
    first = _create(ok_api, port)
    assert first[0] == 201, first
    env_path = dsh_home / "bots" / "xiaoyu" / "channel" / ".env"
    before = (dsh_configs_dir / "xiaoyu.yml").read_bytes()
    env_before = env_path.read_bytes()
    code, body = _create(ok_api, port2, bot_id="xiaoyu")
    assert code == 409, body
    assert body["error"] == "bot_exists"
    assert (dsh_configs_dir / "xiaoyu.yml").read_bytes() == before
    assert env_path.read_bytes() == env_before


@pytest.mark.concurrency
@pytest.mark.slow
def test_加bot_并发两个同端口请求_恰好一个成功一个409(apif, telegram, port, dsh_configs_dir):
    """§4.3 用例⑦ / R4：两个都成功就意味着端口 TOCTOU 没堵住。"""
    telegram.mode = "ok"
    a = apif(HUB_TELEGRAM_API_BASE=telegram.base)
    clients = [a.c, a.app.test_client()]
    results = []
    gate = threading.Barrier(2)

    def go(bot_id, client):
        gate.wait()
        r = client.post(EP, json=addbot_payload(bot_id=bot_id, telegram_token=TOKEN,
                                                dispatcher_port=port))
        results.append((bot_id, r.status_code, r.get_json()))

    ts = [threading.Thread(target=go, args=("aaa_bot", clients[0])),
          threading.Thread(target=go, args=("bbb_bot", clients[1]))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=90)

    assert len(results) == 2, results
    winners = [r for r in results if r[1] in (201, 207)]
    losers = [r for r in results if r[1] == 409]
    assert len(winners) == 1 and len(losers) == 1, results
    assert losers[0][2]["error"] in ("bot_exists", "port_in_use", "config_busy")
    made = [b for b in ("aaa_bot", "bbb_bot") if (dsh_configs_dir / f"{b}.yml").exists()]
    assert made == [winners[0][0]], f"磁盘上产物不止一份：{made}"


@pytest.mark.slow
@pytest.mark.parametrize("bot_id", [
    pytest.param("ab", id="最短两字符"),
    pytest.param("a" + "b" * 31, id="最长32字符"),
])
def test_加bot_botid长度边界合法_创建成功(ok_api, alive_bot, port, dsh_configs_dir,
                                       bot_id):
    alive_bot(bot_id)
    code, body = _create(ok_api, port, bot_id=bot_id)
    assert code == 201, body
    assert (dsh_configs_dir / f"{bot_id}.yml").exists()


@pytest.mark.slow
def test_加bot_显示名是unicode与emoji_原样落盘(ok_api, port, dsh_configs_dir):
    name = "小雨🌧️ちゃん"
    code, body = _create(ok_api, port, display_name=name)
    assert code == 201, body
    assert u.load(dsh_configs_dir / "xiaoyu.yml")["display_name"] == name
