#!/usr/bin/env python3
"""self_initiate.py <bot> <chat_id> — 跨平台主动消息触发器（替代 self-initiate.sh）。

流程：随机间隔闸（30min-24h，开口时机不可预测像活人）→ 调 life-context.py 富情境
判定（SKIP/FALLBACK/TEXT）→ 写 unified inbox → POST /ensure_worker 拉起 worker。

新架构下 inbox 是持久队列（dispatcher 内嵌 worker-manager 监视，ready 后 drain），
旧 bash 版"先 spawn 等 tmux ready 再写 inbox"的 race 修复整个不再需要。
macOS launchd 与 Windows 任务计划都调本脚本（每 10 分钟 tick，闸内自节流）。
"""
import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

DEFAULT_COOLDOWN_MIN_SEC = 1800     # 默认最短间隔 30 分钟（秒）
DEFAULT_COOLDOWN_MAX_SEC = 86400    # 默认最长间隔 24 小时（秒）
# 想念值满（积温 forced）时可以不等随机闸提前开口，但要守这三条
EARLY_MIN_GAP = 3 * 3600   # 距上次成功投递（正常或提前）至少 3 小时
EARLY_DAILY_MAX = 2        # 本地日期当天最多提前投递 2 次
EARLY_MAX_PER_DAY = EARLY_DAILY_MAX  # 别名，保持兼容
EARLY_RETRY = 1800         # 提前那次被 life-context 回 SKIP（或投递失败）后 30 分钟内不再提前


def _valid_positive_int(v) -> bool:
    """正整数校验：int 且非 bool 且 > 0。"""
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def get_proactive_cooldown_range(bot_cfg: dict, global_cfg: dict) -> tuple[int, int]:
    """按 bot yml → 全局 → 默认三级取值，返回 (min_sec, max_sec)。

    键名：proactive_cooldown_min_minutes / proactive_cooldown_max_minutes
    非法值（非正整数）回退默认，min > max 自动交换，返回秒。
    """
    def _resolve(key: str, default_min: int) -> int:
        for cfg in (bot_cfg, global_cfg):
            v = cfg.get(key)
            if _valid_positive_int(v):
                return v
        return default_min

    min_min = _resolve("proactive_cooldown_min_minutes", DEFAULT_COOLDOWN_MIN_SEC // 60)
    max_min = _resolve("proactive_cooldown_max_minutes", DEFAULT_COOLDOWN_MAX_SEC // 60)
    if min_min > max_min:
        min_min, max_min = max_min, min_min
    return min_min * 60, max_min * 60

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.expanduser("~/.claude/dispatcher/.self-initiate-state")

sys.path.insert(0, REPO_ROOT)
from bots_registry import disabled_ids_safe  # noqa: E402  停用判定唯一入口（fail-open 在它内部）


def _bot_port(bot: str) -> str | None:
    """从 configs/_global.yml 之外最简单的来源拿端口：restart 脚本同款约定，
    或环境变量 DISPATCHER_PORT_<BOT>。找不到就返回 None（只写 inbox 不拉 worker）。"""
    env_key = f"DISPATCHER_PORT_{bot.upper()}"
    if os.environ.get(env_key):
        return os.environ[env_key]
    # 默认单 bot 部署：17801
    return "17801"


# ─── 新系统（dsh-bot 网关）的 bot ─────────────────────────────
# 方案 3.9：每次机会都有结果，并按原因安排下一次（不再先重抽周期、再判断发不发）；
# 投递走网关的本机接口（写成账本里的合成消息，由模型在下一轮处理）。

def _dsh_cfg(bot: str) -> dict | None:
    """这个 bot 由新网关在跑就返回它的配置；否则 None（走旧流程）。"""
    try:
        import config_loader
        import chat_history
        cfg = config_loader.load_bot(bot)
        return cfg if chat_history.is_dsh_bot(cfg.get("bot_channel_path")) else None
    except Exception:
        return None


def _next_wake(bot: str) -> int | None:
    try:
        import importlib
        hs = importlib.import_module("hang_situation")
        wakes = hs.plan(bot).get("wakes") or []
        return int(wakes[0]) if wakes else None
    except Exception:
        return None


def retry_after(reason: str, now: int, wake: int | None = None) -> int:
    """跳过之后，过多久再试（秒）。"""
    r = reason or ""
    if "用户" in r:                                   # 对方大概率在睡
        return random.randint(5400, 9000)
    if "sleep" in r or "睡" in r:                      # 她自己在睡：起床后再试
        return max(600, wake - now) if wake and wake > now else 3600
    if "配额" in r:                                    # 额度用完：下个窗口
        return 3600
    if "非静默期" in r or "刚说过" in r or "专注" in r or "moment" in r:  # 刚聊过、正忙
        return random.randint(1800, 3600)
    return random.randint(3600, 7200)                 # 没素材、不想说


def _inject(cfg: dict, chat: str, text: str, key: str) -> bool:
    import gateway_client
    return gateway_client.inject(cfg["bot_channel_path"], chat, text, "self_initiate", key, port=cfg.get("dispatcher_port") or None)


def _read_json(path: str) -> dict:
    """小状态文件读不到/坏了一律当空，不影响主流程。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _save(path: str, data: dict, what: str) -> bool:
    """状态写入一律不抛：失败往 stderr 留一行带异常类名的说明，返回 False 让调用方决定怎么兜底。"""
    try:
        _write_json(path, data)
        return True
    except Exception as e:
        print(f"{what}写入失败 {type(e).__name__}", file=sys.stderr)
        return False


def _early_count(early: dict) -> int:
    c = early.get("count")
    return c if isinstance(c, int) and c > 0 else 0


def _jiwen_forced(bot: str, chat: str) -> bool:
    """积温 forced（想念值满）；关了、没状态文件、坏文件都当不满。"""
    try:
        import config_loader
        from jiwen import reader as jiwen_reader
        info = jiwen_reader.read(bot, chat, config_loader.load_global())
        return bool(info and info.get("forced"))
    except Exception as e:
        print(f"积温读取失败 {type(e).__name__}", file=sys.stderr)
        return False


def _in_sleep(bot: str, cfg: dict, now: int) -> bool:
    """睡眠时段以 load_bot 合并后的配置为准，取不到就用传入的 cfg；判定本身出错当不在睡。"""
    import config_loader
    try:
        bot_cfg = config_loader.load_bot(bot)
    except Exception:
        bot_cfg = cfg or {}
    try:
        return config_loader.in_sleep_hours(bot_cfg, datetime.fromtimestamp(now))
    except Exception as e:
        print(f"睡眠时段判定失败 {type(e).__name__}", file=sys.stderr)
        return False


def _early_block(bot: str, chat: str, now: int, sent: dict, early: dict, cfg: dict | None = None,
                 sent_f: str | None = None) -> str:
    """随机闸没到时能不能提前开口：能返回空串，不能返回原因。睡眠时段最先判，积温最后读。"""
    if _in_sleep(bot, cfg or {}, now):
        return "在睡眠时段"
    last = sent.get("last_sent_at")
    if not isinstance(last, (int, float)) or isinstance(last, bool):
        # 没有上次投递记录（刚上线、记录丢了、新 bot）：先把现在记成上次投递，3 小时后才可能提前，
        # 免得记录一丢就绕开 3 小时间隔
        if sent_f:
            _save(sent_f, {"last_sent_at": now}, "上次投递时刻")
        return "没有上次投递记录，先记下"
    if now - last < EARLY_MIN_GAP:
        return "距上次投递不到 3 小时"
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    if early.get("day") == today and _early_count(early) >= EARLY_DAILY_MAX:
        return "今天提前名额用完"
    skip_at = early.get("last_skip_at")
    if isinstance(skip_at, (int, float)) and now - skip_at < EARLY_RETRY:
        return "提前那次刚被跳过"
    if not _jiwen_forced(bot, chat):
        return "想念值没满"
    return ""


def run_dsh(bot: str, chat: str, cfg: dict, now: int, force: bool = False, skip_judge: bool = False) -> int:
    # 功能开关（约定 v1.1 5.19）："主动找你聊"关着就什么都不做，不问 life-context、不投递、不改下次机会的时间
    from moments import features as feature_switch
    if not feature_switch.enabled(cfg, os.path.expanduser(cfg.get("bot_channel_path") or ""), "proactive"):
        print("skip: 主动找你聊这项功能关着", file=sys.stderr)
        return 0
    state_dir = os.path.join(os.path.dirname(os.path.abspath(os.path.expanduser(cfg["bot_channel_path"]))), "state", "self-initiate")
    os.makedirs(state_dir, exist_ok=True)
    next_f = os.path.join(state_dir, f"{chat}.next")
    try:
        next_at = int(open(next_f, encoding="utf-8").read().strip())
    except Exception:
        next_at = 0
    sent_f = os.path.join(state_dir, f"{chat}.sent.json")    # 上次成功投递时刻（正常、提前都记）
    early_f = os.path.join(state_dir, f"{chat}.early.json")  # 提前开口记账：当天次数、上次被跳过的时刻
    sent, early_state = _read_json(sent_f), _read_json(early_f)
    early = False
    if not force and now < next_at:
        why = "手动测试不走提前" if skip_judge else _early_block(bot, chat, now, sent, early_state, cfg, sent_f)
        if why:
            print(f"skip: 还没到下次机会（还有 {(next_at - now) // 60} 分钟；不提前：{why}）", file=sys.stderr)
            return 0
        early = True
        print("想念值满，随机闸没到但提前问一次 life-context", file=sys.stderr)

    def early_skipped(why: str) -> int:
        # 提前那次没成：不动 .next（随机闸原定时间不被推后），记下时刻，30 分钟内不再提前
        early_state["last_skip_at"] = now
        _save(early_f, early_state, "提前记账")
        print(f"{why}（提前那次，下次机会时间不变）", file=sys.stderr)
        return 0

    def schedule(sec: int, why: str) -> int:
        try:
            with open(next_f, "w", encoding="utf-8") as f:
                f.write(str(now + sec))
        except Exception as e:
            print(f"下次机会时间写入失败 {type(e).__name__}", file=sys.stderr)
        print(f"{why}；下次机会在 {sec // 60} 分钟后", file=sys.stderr)
        return 0

    hour = datetime.now().hour
    text = f"⟦系统·主动开口⟧ 现在 {hour} 点，你可以按自己的心情主动找对方说点什么，也可以不说（用 stay_silent）。"
    if skip_judge:  # 只给手动测试用：不问 life-context，直接把默认文本投给网关
        try:
            ok = _inject(cfg, chat, text, f"{chat}:{now}:test")
        except Exception as e:
            print(f"投递失败（网关没在跑？）：{type(e).__name__}", file=sys.stderr)
            return 1
        print("已投递给网关（测试，跳过了判断，不改下次机会的时间）" if ok else "投递被拒", file=sys.stderr)
        return 0 if ok else 1
    # 提前那次带标记，让 life-context 照跑硬规则预筛、只跳过判官；正常路径不传 env（与原来一致）
    extra = {"env": {**os.environ, "SELF_INITIATE_EARLY": "1"}} if early else {}
    try:
        r = subprocess.run([sys.executable, os.path.join(REPO_ROOT, "life-context.py"), bot, chat],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, cwd=REPO_ROOT,
                           **extra)
        out = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip() else {"action": "FALLBACK"}
    except Exception as e:
        print(f"life-context error {type(e).__name__} → 用 FALLBACK 文本", file=sys.stderr)
        out = {"action": "FALLBACK"}
    action = out.get("action") if isinstance(out, dict) else "FALLBACK"
    if early and action != "TEXT":
        # 提前那次只认 TEXT：FALLBACK、出错、空输出、坏 JSON、超时都不投递（只有正常路径才用默认文本兜底）
        return early_skipped(f"skip: life-context 回 {action}" + (f"（{out.get('reason', '')}）" if isinstance(out, dict) and out.get("reason") else ""))
    if action == "SKIP":
        reason = str(out.get("reason", ""))
        wake = _next_wake(bot) if ("sleep" in reason or "睡" in reason) and "用户" not in reason else None
        return schedule(retry_after(reason, now, wake), f"skip: {reason}")
    if action == "TEXT":
        text = "⟦系统·主动开口⟧ " + str(out.get("text", ""))[:4000]
    if early:
        # 提前那次先记账再投递：记账写不进去就不发。投出去之后才写，一旦写失败，
        # 3 小时间隔和每日名额就守不住，会连发；先记了再投递失败，最多保守多等 3 小时
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        count = _early_count(early_state) if early_state.get("day") == today else 0
        if not _save(sent_f, {"last_sent_at": now}, "上次投递时刻"):
            return early_skipped("记账写不进去，不提前")
        early_state.update(day=today, count=count + 1)
        if not _save(early_f, early_state, "提前记账"):
            return early_skipped("记账写不进去，不提前")
    try:
        ok = _inject(cfg, chat, text, f"{chat}:{now}")
    except Exception as e:
        why = f"投递失败（网关没在跑？）：{type(e).__name__}"
        return early_skipped(why) if early else schedule(600, why)
    if not ok:
        return early_skipped("投递被拒") if early else schedule(600, "投递被拒")
    if early:
        # 提前投出去了（账已先记好）：随机闸下次机会不早于 3 小时后，让 3 小时间隔对正常那条路也生效；
        # 取 max 不把原定更晚的随机闸提前
        return schedule(max(next_at, now + EARLY_MIN_GAP) - now, f"已提前投递给网关（今天第 {count + 1} 次提前）")
    # 记上次投递时刻失败只记日志，不能挡住后面写 .next
    _save(sent_f, {"last_sent_at": now}, "上次投递时刻")
    # 真正投出去了：下一次机会按随机间隔（开口时机不可预测，像活人）
    try:
        import config_loader as _cl
        _global_cfg = _cl.load_global()
    except Exception:
        _global_cfg = {}
    cd_min, cd_max = get_proactive_cooldown_range(cfg, _global_cfg)
    return schedule(random.randint(cd_min, cd_max), "已投递给网关")


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: self_initiate.py <bot> <chat_id> [--force] [--skip-judge]", file=sys.stderr)
        return 2
    bot, chat = sys.argv[1], sys.argv[2]
    now = int(os.environ.get("SELF_INITIATE_NOW") or time.time())  # 时钟注入（测试用）
    hour = datetime.now().hour

    # ─── 随机间隔闸：到点才允许，然后立刻 roll 下一个周期 ───
    os.makedirs(STATE_DIR, exist_ok=True)
    marker = os.path.join(STATE_DIR, f"{bot}-{chat}.last")
    interval_f = os.path.join(STATE_DIR, f"{bot}-{chat}.interval")

    # ─── 需求⑤：停用的 bot 计划任务照常触发，这里自行空退出（不写 inbox、不 POST）───
    # 先刷 .last（不动 .interval）：启用后随机间隔从最后一轮停用 tick 重新计，不会一启用就补发主动消息。
    dsh = _dsh_cfg(bot)
    if dsh is not None and bot not in disabled_ids_safe():
        return run_dsh(bot, chat, dsh, now, force="--force" in sys.argv[3:], skip_judge="--skip-judge" in sys.argv[3:])
    if bot in disabled_ids_safe():
        print(f"skip: {bot} disabled (configs/{bot}.yml enabled:false)", file=sys.stderr)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(now))
        return 0
    try:
        last = int(open(marker, encoding="utf-8").read().strip())
    except Exception:
        last = 0
    try:
        import config_loader as _cl
        _bot_cfg = _cl.load_bot(bot)
        _global_cfg = _cl.load_global()
    except Exception:
        _bot_cfg, _global_cfg = {}, {}
    cd_min, cd_max = get_proactive_cooldown_range(_bot_cfg, _global_cfg)
    try:
        target = int(open(interval_f, encoding="utf-8").read().strip())
    except Exception:
        target = cd_min
    if last and now - last < target:
        print(f"skip: 距上次 {now-last}s < 随机目标 {target}s", file=sys.stderr)
        return 0
    open(marker, "w", encoding="utf-8").write(str(now))
    open(interval_f, "w", encoding="utf-8").write(str(random.randint(cd_min, cd_max)))

    bot_dir = os.path.expanduser(f"~/.claude/channels/{bot}")
    inbox = os.path.join(bot_dir, "inbox")
    os.makedirs(inbox, exist_ok=True)

    # ─── since_last_user_msg_min（dispatcher 写的 marker）───
    since_min = "unknown"
    lu = os.path.join(STATE_DIR, f"{bot}-{chat}.last-user")
    try:
        since_min = str((now - int(open(lu, encoding="utf-8").read().strip())) // 60)
    except Exception:
        pass

    # ─── life-context 富情境判定 ───
    life_ctx = os.path.join(REPO_ROOT, "life-context.py")
    text = f"[self-initiate] hour={hour} since_last_user_msg_min={since_min}"
    if os.path.isfile(life_ctx):
        try:
            r = subprocess.run([sys.executable, life_ctx, bot, chat],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, cwd=REPO_ROOT)
            if r.returncode != 0:
                print(f"life-context exit={r.returncode} → skip", file=sys.stderr)
                return 0
            out = json.loads(r.stdout.strip().splitlines()[-1])
            action = out.get("action")
            if action == "SKIP":
                print(f"skip per life-context: {out.get('reason','')}", file=sys.stderr)
                return 0
            if action == "TEXT":
                text = str(out.get("text", ""))[:4000]
            elif action != "FALLBACK":
                print(f"unknown action {action} → skip", file=sys.stderr)
                return 0
        except Exception as e:
            print(f"life-context error {type(e).__name__}: {e} → 用 FALLBACK 文本", file=sys.stderr)

    # ─── 防积压：清掉本 bot 之前未消费的 self-*（只有最新一条有意义）───
    for f in os.listdir(inbox):
        if f.startswith("self-") and f.endswith(".json"):
            try: os.remove(os.path.join(inbox, f))
            except OSError: pass

    # ─── 写 unified inbox（schema 与 dispatcher 真实 meta 对齐）───
    ms = int(time.time() * 1000)
    payload = {
        "text": text,
        "chat_id": chat,
        "from_id": chat,
        "from_username": "user",
        "sender_username": "user",
        "chat_type": "private",
        "scene": "private",
        "is_bot_sender": False,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message_id": str(ms),
    }
    fname = os.path.join(inbox, f"self-{ms}.json")
    tmp = fname + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, fname)
    print(f"wrote {fname}", file=sys.stderr)

    # ─── 拉起 worker（dispatcher /ensure_worker；不在也没事，inbox 会被 drain）───
    port = _bot_port(bot)
    if port:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ensure_worker", method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"ensure_worker 失败(dispatcher 未起?): {e}", file=sys.stderr)

    # ─── quota 计费 ───
    try:
        sys.path.insert(0, REPO_ROOT)
        import quota
        quota.record_call("worker_trigger", quota.WORKER_TRIGGER_WEIGHT)
    except Exception:
        print("quota record_call failed (non-fatal)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
