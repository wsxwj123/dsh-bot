#!/usr/bin/env python3
"""重启单只 bot 的 worker（kill + 预热）+ 收敛器（无 Flask 依赖）。

只 kill 匹配 ``^tg-<bot>-(worker|-?[0-9]+)$`` 的会话，**绝不**碰 ``tg-<bot>-dispatcher``
（形状照抄 provider-refresh.py:39 的 WORKER_RE）。INTERFACE §1.3 / §6。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid as _uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import bot_stop  # noqa: E402
import legacy_config_loader as config_loader  # noqa: E402  旧系统语义（yml 直读 + 旧仓默认路径）
import provider_config as pc  # noqa: E402
import retry_backoff  # noqa: E402

# 本模块是旧系统的 **POSIX 机制**（tmux 会话 + spawn-worker.sh + TMUX_TMPDIR=/tmp）。
# Windows 上没有 tmux/bash：外部命令调用会 OSError，`_tmux` 一族按"会话不在"、
# 重启按"拉起失败（spawn_failed）"安全降级，异常不会穿到请求层；其余逻辑
# （闲忙判定、退避、记账）不依赖 POSIX。端口与目录仍由 BOT_PORTS / 接缝 env 控制。
SPAWN_SH = os.environ.get("CLAUDEBOT_SPAWN_SH") or os.path.join(
    os.path.expanduser("~"), ".claude", "dispatcher", "spawn-worker.sh")
TMUX_BIN = os.environ.get("CLAUDEBOT_TMUX_BIN") or shutil.which("tmux") or "tmux"
_subprocess = subprocess  # 外部命令注入点

_IDLE_JSONL_SEC = 15
# worker-state 陈旧阈值（秒）：与权威读者 ~/.claude/dispatcher/worker-idle-wait.sh 的 STALE_MS 对齐
_STATE_STALE_SEC = 15 * 60
# 清单写不进去时的进程内退避表（bot_id → 退避片段）：清单落盘失败也不能回到每 10 秒重拉一次
_mem_backoff: dict[str, dict] = {}


def _worker_re(bot_id: str) -> re.Pattern:
    return re.compile(r"^tg-%s-(worker|-?[0-9]+)$" % re.escape(bot_id))


def _tmux(args: list[str], timeout: float = 5.0):
    return _subprocess.run([TMUX_BIN, *args],
                           env={**os.environ, "TMUX_TMPDIR": "/tmp"},
                           capture_output=True, text=True, timeout=timeout)


def _session_exists(bot_id: str) -> bool:
    try:
        return _tmux(["has-session", "-t", "tg-%s-worker" % bot_id]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _list_sessions() -> list[str]:
    try:
        r = _tmux(["ls", "-F", "#S"])
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [s for s in r.stdout.splitlines() if s.strip()]


def _is_idle(bot_id: str) -> bool:
    """空闲门（INTERFACE-d18 §一）：杀 worker 前四件事都要静。

    - 统一 inbox/*.json 空 + inbox/.pending/*.json 空（任一非空 = 有未投递消息 = 忙）；
    - per-chat inbox（chats/<chat>/inbox 及其 .pending）同样为空（回评/生图这类消息的落点）；
    - worker 忙标记 .worker-state.json：busy=true 且新鲜才算忙（陈旧/缺失不据此判忙）；
    - worker **在**时再看 unified jsonl mtime 距今 > 15 秒；
    - worker **不在**时不看 mtime（没有在写的会话），但仍要求上面几处为空：
      不在且 inbox 非空 = 忙（此刻 kill+spawn 会与未消费消息抢跑，也无从证明没有回复被打断）。
    """
    d = pc._bot_dir(bot_id)
    if not d:
        return True
    if _inbox_pending(bot_id):
        return False
    if _per_chat_inbox_pending(bot_id):
        return False
    if _worker_busy(bot_id):
        return False
    if not _session_exists(bot_id):
        return True
    path = pc.unified_jsonl_path(bot_id)
    if path and os.path.exists(path):
        try:
            if time.time() - os.path.getmtime(path) <= _IDLE_JSONL_SEC:
                return False
        except OSError:
            pass
    return True


def _inbox_dir_pending(inbox: str) -> bool:
    """inbox 目录与其 .pending 子目录里是否还有 *.json。"""
    for sub in (inbox, os.path.join(inbox, ".pending")):
        try:
            if any(n.endswith(".json") for n in os.listdir(sub)):
                return True
        except OSError:
            continue
    return False


def _inbox_pending(bot_id: str) -> bool:
    """inbox / inbox/.pending 里是否还有未投递的消息。"""
    d = pc._bot_dir(bot_id)
    if not d:
        return False
    return _inbox_dir_pending(os.path.join(d, "inbox"))


def _per_chat_inbox_pending(bot_id: str) -> bool:
    """chats/<chat>/inbox 及其 .pending 里是否还有未投递的消息（INTERFACE-d18 §一.1b）。

    写入方为 moments/post.py 与 moments/web.py（回评、生图这类 per-chat 消息的落点）。
    扫 chats/ 下**全部** chat 目录：不知道待处理消息来自哪个 chat，漏一处就是杀回合。
    """
    d = pc._bot_dir(bot_id)
    if not d:
        return False
    chats_root = os.path.join(d, "chats")
    try:
        names = os.listdir(chats_root)
    except OSError:
        return False
    return any(_inbox_dir_pending(os.path.join(chats_root, n, "inbox")) for n in names)


def _worker_busy(bot_id: str) -> bool:
    """worker 忙标记（INTERFACE-d18 §一.1d）：`.worker-state.json` busy=true 且新鲜才算忙。

    口径与权威读者 ~/.claude/dispatcher/worker-idle-wait.sh 对齐：
    - 文件缺失 / 读不出 / 非对象 / v!=1 / busy 非 true → 不忙（对老 worker 零误判）；
    - 新鲜度以文件内 `at`（毫秒）为准；at 解析不出回退文件 mtime；时钟回拨按刚到；
      超过 15 分钟（_STATE_STALE_SEC）→ 陈旧，不据此判忙。
    """
    d = pc._bot_dir(bot_id)
    if not d:
        return False
    path = os.path.join(d, ".worker-state.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict) or state.get("v") != 1 or state.get("busy") is not True:
        return False
    at = state.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        try:
            at = os.path.getmtime(path) * 1000.0
        except OSError:
            return False
    age_ms = time.time() * 1000.0 - at
    if age_ms < 0:
        age_ms = 0.0  # 时钟回拨：当作刚到
    return age_ms <= _STATE_STALE_SEC * 1000


def _wait_idle(bot_id: str, wait_sec: int) -> bool:
    """每 2 秒重查 `_is_idle`，最多 wait_sec；超时 → 调用方转 queued（不杀不拉）。"""
    deadline = time.time() + max(0, wait_sec)
    while True:
        if _is_idle(bot_id):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(2)


def _kill_worker(bot_id: str) -> tuple[list[str], list[str]]:
    """kill 本 bot 的 worker 会话，返回 (已确认消失的会话, 重试后仍在的会话)。

    kill 的成败**以会话表复核为准**（不是只看命令退出码，也不是"try 没抛就算成功"）：
    ``spawn-worker.sh`` 对已存在的会话是 no-op（幂等），旧的没杀掉却报成功 = 用户以为
    provider 换了、实际那只 bot 还在用旧环境跑 —— 正是本功能要防的静默失效。
    """
    rx = _worker_re(bot_id)
    live = [n for n in _list_sessions() if rx.match(n)]
    left = list(live)
    for _ in range(2):  # 等 0.5s 确认消失；仍在 → 重试一次（§6.1 step 5）
        for name in live:
            try:
                _tmux(["kill-session", "-t", name])
            except (OSError, subprocess.SubprocessError):
                pass
        time.sleep(0.5)
        left = [n for n in _list_sessions() if rx.match(n)]
        if not left:
            break
    return [n for n in live if n not in left], left


def _alive(bot_id: str) -> bool:
    """只认 tmux has-session 退出码 0；tmux 不存在/超时/抛异常一律按不在。"""
    try:
        return bool(_session_exists(bot_id))
    except Exception:
        return False


def _cursor_line(bot_id: str):
    """capture-pane 抓屏取"光标行"（与 spawn-worker.sh 的 D-02 识别同款）：

    光标行 = 第一个含 `❯` 的行；没有则第一个以 `>` 开头的行。
    读不出（tmux 不在 / 超时 / 非 0 退出）→ None（"说不清"，调用方回退现状）。
    """
    try:
        r = _tmux(["capture-pane", "-t", "tg-%s-worker" % bot_id, "-p"])
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    lines = (r.stdout or "").splitlines()
    for ln in lines:
        if "❯" in ln:
            return ln
    for ln in lines:
        if ln.startswith(">"):
            return ln
    return None


def _startup_dialog_on_screen(bot_id: str) -> bool:
    """启动对话框特征（INTERFACE-d18 §二.3）：光标行里，提示符之后还有文字。

    与 spawn-worker.sh D-02 同款口径：只看光标行本身（整屏别处出现什么都不算）。
    - 光标行不存在 / 读不出 → False：说不清，回退"会话在=就绪"（不误杀慢启动会话）；
    - 裸提示符（`❯` / `>` 之后只有空白）→ False：已过对话框，正常界面；
    - 提示符后还有文字（选项行，如 `❯ 1. Yes, ...`）→ True：卡在启动对话框。
    绝不在整屏搜 Error / 报错字样——判据只有这一行。
    """
    line = _cursor_line(bot_id)
    if line is None:
        return False
    if "❯" in line:
        rest = line.split("❯", 1)[1]
    else:
        rest = line[1:]  # _cursor_line 只会返回含 ❯ 的行或以 > 开头的行
    return rest.strip() != ""


def _wait_ready(bot_id: str, ready_wait_sec: int) -> tuple[bool, str]:
    """就绪判据（INTERFACE-d18 §二）：会话在，且不卡在启动对话框。

    - 会话出现且 3 秒后复查仍在（防"起即退"）；
    - 复查通过后读光标行（D-02 同款）：启动对话框特征 → 未就绪（失败语义）；
      裸提示符 → 就绪；说不清（无光标行 / 读不出）→ 回退"会话在=就绪"；
    - 绝不在整屏搜错误词（历史重绘会误判，PLAN P2）。
    """
    wait = max(0, ready_wait_sec)
    deadline = time.time() + wait
    time.sleep(min(3, wait))  # 给 spawn 一点启动时间
    while True:
        if not _alive(bot_id):
            if time.time() >= deadline:
                return False, "worker 会话未出现"
            time.sleep(3)
            continue
        time.sleep(3)
        if _alive(bot_id):
            if _startup_dialog_on_screen(bot_id):
                return False, "worker 卡在启动对话框"
            return True, "worker 已就绪"
        if time.time() >= deadline:
            return False, "worker 会话起后即消失"
        # 未到时限：立即再探测（消失后又出现并连续两次都在，照样算就绪）


def _reap(proc) -> None:
    """收尾 spawn-worker.sh 子进程，避免已退出的它变僵尸。

    原先只在成功分支 ``poll()``，失败分支直接 return → 收敛器每 10 秒重试一次、一只永久
    坏的 bot 会无限累积僵尸。``poll()``（已退出者回收退出状态）非阻塞；仍在跑（罕见）时
    有界等待，绝不无限阻塞请求线程。
    """
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=2)
    except (_subprocess.TimeoutExpired, OSError):
        pass


def restart_bot_worker(bot_id: str, *, force: bool = False,
                       idle_wait_sec: int = 20, ready_wait_sec: int = 20) -> dict:
    """kill + 预热该 bot 的 worker。永远 prewarm（killed_only 已删）。

    判定顺序（I-3）：认不出 → 已停用 → 能不能拉起的三项预检（零副作用）→ 空闲门 → kill/spawn。
    """
    for name, v in (("idle_wait_sec", idle_wait_sec), ("ready_wait_sec", ready_wait_sec)):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError("%s 必须是 int/float" % name)
    idle_wait_sec = max(0, idle_wait_sec)
    ready_wait_sec = max(0, ready_wait_sec)
    start = time.time()

    def ret(status, detail, killed=None, spawned=False, strip=False, forced=False):
        return {
            "bot_id": bot_id,
            "status": status,
            "detail": detail,
            "session": "tg-%s-worker" % bot_id,
            "killed_sessions": killed or [],
            "spawned": spawned,
            "elapsed_ms": int((time.time() - start) * 1000),
            "strip_thinking": strip,
            "forced": forced,
        }

    # 名单与写口（set_bot_provider / 面板）同一集合：yml ∪ 目录，否则无 yml 的 bot 能被打标记却永远消化不了
    if not isinstance(bot_id, str) or bot_id not in pc._discovered_bot_ids():
        return ret("unknown_bot", "不在已发现的 bot 列表")
    if bot_stop.is_stopped(bot_id):
        return ret("stopped", "bot 已停用，不杀不拉")
    # 预检放在空闲门与 kill 之前：拉不起来的 bot 不能先把旧 worker 杀了
    try:
        cfg = config_loader.load_bot(bot_id)
    except FileNotFoundError:
        return ret("unrestartable", "bot 没有配置文件，面板无法重启")
    except Exception:
        cfg = None
    if not isinstance(cfg, dict):
        return ret("spawn_failed", "bot 配置暂时读不出")  # 文件在、这一刻读不出：可恢复，走退避
    chat_id = cfg.get("chat_id")
    if chat_id in (None, ""):
        return ret("unrestartable", "bot 配置缺少 chat_id")
    port = pc.BOT_PORTS.get(bot_id)
    if not port:
        return ret("unrestartable", "未知 bot 端口")

    with pc.bot_lock(bot_id):
        if not force and not _wait_idle(bot_id, idle_wait_sec):
            return ret("queued", "busy：%s 秒内未空闲，保留待重启" % idle_wait_sec)

        strip = pc.mark_thinking_stale(bot_id)
        killed, left = _kill_worker(bot_id)
        if left:
            # 旧的没杀掉就**不许拉起新的**：spawn-worker.sh 幂等，拉起来的"新"worker 其实
            # 还是带旧环境的老进程 —— 报成功就是假成功（契约 §6.1 step 5 要求确认消失）。
            return ret("spawn_failed",
                       "旧 worker 未杀掉，已放弃本次重启（仍在: %s）" % ",".join(left),
                       killed=killed, strip=strip)

        try:
            proc = _subprocess.Popen(
                ["bash", SPAWN_SH, bot_id, str(chat_id), str(_uuid.uuid4()),
                 "http://127.0.0.1:%s" % port],
                env={**os.environ, "TMUX_TMPDIR": "/tmp"})
        except OSError as e:
            return ret("spawn_failed", "拉起失败: %s" % type(e).__name__,
                       killed=killed, strip=strip)

        try:
            ok, detail = _wait_ready(bot_id, ready_wait_sec)
            code = proc.poll()   # 顺带回收：spawn-worker.sh 起完 tmux 即退出
            if not ok:
                return ret("spawn_failed", detail, killed=killed, spawned=True, strip=strip)
            if code is not None and code != 0:
                return ret("spawn_failed", "spawn-worker.sh 退出码 %s" % code,
                           killed=killed, spawned=True, strip=strip)
            return ret("ok", "worker 已带新 provider 起来",
                       killed=killed, spawned=True, strip=strip, forced=force)
        finally:
            _reap(proc)


def converge_once(*, max_bots: int = 1, force_after_sec: int = 1800) -> dict:
    """收敛器单步：按时序处理最多 max_bots 只 pending（不并发杀多只）。

    **接缝不全（只设了既有 CLAUDEBOT_CONFIG_DIR）→ 不读 providers.json、直接返回空结果**：
    那种环境下 ``PROVIDERS_PATH`` 会落回生产的 ``configs/providers.json``，读它等于拿生产的
    待重启项去 kill/spawn 真 worker（INTERFACE §1.1 副作用入口闸）。
    判据用 ``seams_read_only()``（"接缝不全"）而非 ``not seams_complete()``：后者在生产态也是
    True，而生产态**必须**能收敛（见 moments/web.py::_start_converger 同款说明）。
    """
    if isinstance(max_bots, bool) or not isinstance(max_bots, int):
        raise TypeError("max_bots 必须是 int")
    if isinstance(force_after_sec, bool) or not isinstance(force_after_sec, (int, float)):
        raise TypeError("force_after_sec 必须是 int/float")
    if pc.seams_read_only():
        return {"processed": [], "pending_left": 0}
    processed = []
    used = 0  # 本轮实际杀/拉的只数（ok / spawn_failed 才占名额）
    now = time.time()
    pending = pc.list_pending()
    # force_after_sec <= 0 或 NaN → 本轮不强杀
    can_force = force_after_sec > 0 and force_after_sec == force_after_sec
    for entry in pending:
        if used >= max_bots:
            break
        bot_id = entry["bot_id"]
        # 已停止自动重试、清单退避未到期、进程内退避未到期 → 跳过，不进 processed
        if not retry_backoff.is_due(entry, now) or not retry_backoff.is_due(_mem_backoff.get(bot_id), now):
            continue
        # 强杀只对"因忙排队太久"的项：失败过（fail_count>0）或从未因忙排队的项不强杀（拍板 5）
        qs = entry["queued_since"]
        force = bool(can_force and qs is not None and now - qs >= force_after_sec
                     and entry["fail_count"] == 0)
        try:
            res = restart_bot_worker(bot_id, force=force, idle_wait_sec=0)
        except Exception as e:  # 一只 bot 抛异常不影响同一轮里后面的 bot
            res = {"bot_id": bot_id, "status": "spawn_failed",
                   "detail": "内部错误：%s" % type(e).__name__}
        status = res["status"]
        cleared = settle(bot_id, res, forced=force)  # 记账写不进去 → 原样上抛，本轮终止
        if status in ("ok", "spawn_failed"):
            used += 1
        processed.append({"bot_id": bot_id, "status": status,
                          "cleared": cleared, "forced": force})
    return {"processed": processed, "pending_left": len(pc.list_pending())}


def _is_orphan(bot_id: str) -> bool:
    """确认标记没有保护对象：channels 根可读、其下没有该 bot 的目录、配置目录下也没有它的 yml。
    根目录读不了时发现列表会临时缺项，确认不了就不算孤儿。"""
    try:
        names = set(os.listdir(pc.CHANNELS_ROOT))
    except OSError:
        return False
    if bot_id in names or os.path.isdir(os.path.join(pc.CHANNELS_ROOT, bot_id)):
        return False
    return not os.path.exists(os.path.join(config_loader.config_dir(), "%s.yml" % bot_id))


def settle(bot_id: str, res: dict, *, forced: bool = False) -> bool:
    """重启结果的唯一记账入口（收敛器与面板 R7/R8/R9 共用）。返回标记是否已清除。

    记账落盘失败：把该 bot 记进进程内退避表后把异常原样上抛；任一次记账成功即移出退避表。
    """
    status = res.get("status")
    detail = res.get("detail") or ""
    cleared = False
    try:
        if status == "ok":
            pc.set_applied(bot_id)
            if forced:
                pc.note_forced(bot_id)
            cleared = True
        elif status == "spawn_failed":
            pc.record_failure(bot_id, detail)
        elif status == "queued":
            pc.note_queued(bot_id)  # 只在首次因忙排队时落值，强杀从这一刻起算
        elif status == "stopped":
            pc.clear_pending(bot_id)
            cleared = True
        elif status == "unrestartable":
            pc.halt_restart(bot_id, detail)  # 停止自动重试，标记保留等人工
        elif status == "unknown_bot":
            if not _is_orphan(bot_id):
                return False  # 确认不了：不记账，标记保留，下一轮再看
            pc.clear_pending(bot_id)
            sys.stderr.write("[restart] 清除孤儿待重启标记 bot=%s\n" % bot_id)
            cleared = True
        else:
            return False
    except Exception:
        _mem_backoff[bot_id] = retry_backoff.on_failure(
            _mem_backoff.get(bot_id), time.time(),
            base=pc.RESTART_BACKOFF_BASE_SEC, cap=pc.RESTART_BACKOFF_CAP_SEC)
        raise
    _mem_backoff.pop(bot_id, None)
    return cleared


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    if "--converge" in flags:
        out = converge_once()
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not args:
        sys.stderr.write("usage: restart_bot_worker.py <bot> [--force] [--converge]\n")
        return 1
    res = restart_bot_worker(args[0], force="--force" in flags)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["status"] in ("ok", "queued", "stopped") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
