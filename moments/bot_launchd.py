# -*- coding: utf-8 -*-
"""dsh bot 的 launchd 操作与判活（INTERFACE §5 / §5b）。

**唯一实现**：重启判成、「/hub/api/bots」的 dsh 行、新建流程的 restart 步骤、
开关触发的单只同步全用这一份，调用方不许各判一遍。机器级动作只碰
``com.dsh-bot.`` 前缀的单元；旧系统的 tmux 会话、``com.wsxwj.*`` 单元一律不碰。

接缝（§1，全部经环境变量，测试注入假件）：

- ``HUB_LAUNCHCTL``（默认 ``/bin/launchctl``）：**绝不真调**，测试把它指到假件，
  默认接缝在生产里就是系统那支；
- ``HUB_LAUNCHAGENTS_DIR``（默认 ``~/Library/LaunchAgents``）：回落 bootstrap 与
  「单元装没装」判定都经它；
- ``DSH_BOT_HOME``（经 ``config_sources.dsh_home``）：心跳文件在
  ``<它>/bots/<bot>/state/heartbeat``。

**受理时定下接缝**（§1a）：重启全部、单只重启、开关同步都是先回响应、后台线程接着做。
后台那一段不许再读环境变量与配置文件，所以三条路径在受理那一刻由 ``resolve_seams()``
解析成一个 ``Seams``，随 job 执行体一路往下传。读环境只留在 ``launchctl_path`` /
``launchagents_dir`` / ``bot_home`` 三个取路径的函数里；其余函数只接收、只往下传
``seams``，自己不读环境。``seams`` 缺省（同步调用，例如新建流程的等活）时现读。

判活（§5）只回答一个问题：有没有**确凿的新鲜心跳**。用 launchctl 的退出码或
kickstart 的返回值当成功证据都是撒谎（进程可能秒退），所以这里要心跳加 ``print``
双证据；判活失败一定带得出人话原因，供 tail 与 manual_hint 用。
"""
import json
import os
import re
import subprocess
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LAUNCHCTL_TIMEOUT = 10.0        # I4：单次 launchctl 调用上限（超时按该只失败）
HEARTBEAT_FRESH_MS = 15_000     # §5 第 3 条：心跳新鲜阈值
RESTART_WAIT = 45.0             # §5：重启/开关同步的等活上限
CREATE_WAIT = 30.0              # §6：新建 bot 的探活上限
POLL_INTERVAL = 0.25            # 等活轮询间隔
MAX_WAIT_WORKERS = 8            # 多只并行等的线程上限（bot 数量是个位数）

#: `print` 解析（§5 第 4 条）：只认**行首恰好一个制表符**的 `state =` / `pid =` 两行。
_PRINT_RE = re.compile(r"^\t(state|pid) = ([^\n]*)$", re.M)

#: 受理那一刻定下的三条最终路径（§1a）：launchctl 可执行文件、plist 目录、新系统家目录。
#: 只读；后台线程拿着它执行，不再读环境。
Seams = namedtuple("Seams", "launchctl agents_dir home")


# ---------------------------------------------------------------- 路径接缝

def resolve_seams():
    """**受理那一刻**把后台要用的三条路径定下来（§1a），返回只读的 ``Seams``。

    只在处理请求的线程里调（回 202 / 200 之前，或列表接口提交探活任务之前）。默认值与
    ``~`` 的展开在这里做完，两个目录落成绝对路径 —— 之后环境变量、``HOME``、当前目录
    怎么变，都影响不到已经拿着这份对象的后台线程。launchctl 那条原样保留（它可以是一个
    要走 PATH 查找的命令名）。
    """
    from moments import config_sources
    return Seams(launchctl=launchctl_path(),
                 agents_dir=os.path.abspath(launchagents_dir()),
                 home=Path(os.path.abspath(config_sources.dsh_home())))


def launchctl_path(seams=None):
    """``HUB_LAUNCHCTL``，默认系统那支。测试注入假件时绝不落回真实 launchctl。

    给了 ``seams`` 就用受理时定下的那支，不读环境。
    """
    if seams is not None:
        return seams.launchctl
    return os.environ.get("HUB_LAUNCHCTL") or "/bin/launchctl"


def launchagents_dir(seams=None):
    """plist 目录：``HUB_LAUNCHAGENTS_DIR``，默认 ``~/Library/LaunchAgents``。

    给了 ``seams`` 就用受理时定下的那个目录，不读环境。
    """
    if seams is not None:
        return seams.agents_dir
    return (os.environ.get("HUB_LAUNCHAGENTS_DIR")
            or os.path.expanduser("~/Library/LaunchAgents"))


def bot_home(bot_id, seams=None):
    """``<DSH_BOT_HOME>/bots/<bot>``；名字过长/含路径分隔符回 None（见 config_sources）。

    给了 ``seams`` 就拼在受理时定下的家目录下，不读环境；bot id 形状那道闸照旧过
    （与 ``config_sources.dsh_bot_dir`` 同一把尺，不许拿不是 bot id 的串去拼路径）。
    """
    from moments import config_sources
    if seams is None:
        return config_sources.dsh_bot_dir(bot_id)
    if not isinstance(bot_id, str) or not config_sources._BOT_ID_RE.match(bot_id):
        return None
    return Path(seams.home) / "bots" / bot_id


def log_path(bot_id, seams=None):
    """失败 tail 里要带的那条日志路径（§8 第 4 条）。"""
    home = bot_home(bot_id, seams)
    if home is None:
        return ""
    return str(home / "logs" / "launchd.log")


def label_of(bot_id):
    return "com.dsh-bot.%s" % bot_id


def _uid():
    """launchd 的 gui 域按当前用户拼；非 Unix 平台没有 getuid，回 0 而不是抛。"""
    fn = getattr(os, "getuid", None)
    return fn() if callable(fn) else 0


# ---------------------------------------------------------------- 动作（§3 第 2、3 步）

def _run(args, timeout=None, seams=None):
    """跑一条 launchctl，stdout 与 stderr **合并**采集（launchd 的报错在 stderr）。

    返回 ``(exit_code, text, why)``：why ∈ None / ``timeout`` / ``spawn``。
    超时交给 ``subprocess.run`` 杀掉子进程 —— 一次 hang 不许把 job 线程与
    ``RestartJobs`` 互斥一起卡死。
    """
    limit = LAUNCHCTL_TIMEOUT if timeout is None else timeout
    try:
        proc = subprocess.run([launchctl_path(seams)] + [str(a) for a in args],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=limit)
    except subprocess.TimeoutExpired:
        return None, "", "timeout"
    except OSError as e:
        # 不回显命令串（含路径与 label），与 §7 的异常口径一致
        return None, type(e).__name__, "spawn"
    return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace"), None


def kickstart(bot_id, seams=None):
    """``kickstart -k``。返回 ``(status, text)``，status ∈ ok / missing / timeout / failed。

    ``missing``（输出含 ``Could not find service``）= 单元没加载，要回落 bootstrap；
    真机实测这种失败退出码是 113，所以按文本判，不按码。
    """
    code, text, why = _run(["kickstart", "-k", "gui/%d/%s" % (_uid(), label_of(bot_id))],
                           seams=seams)
    if why == "timeout":
        return "timeout", text
    if why:
        return "failed", text
    if code == 0:
        return "ok", text
    if "Could not find service" in text:
        return "missing", text
    return "failed", text


def bootstrap(bot_id, seams=None):
    """``bootstrap``（回落路径）。返回 ``(status, text)``，status ∈ ok / no_plist / timeout / failed。

    plist 不在场就不动手：没有单元文件时 bootstrap 只会报一句没用的错。
    """
    plist = os.path.join(launchagents_dir(seams), label_of(bot_id) + ".plist")
    if not os.path.isfile(plist):
        return "no_plist", ""
    code, text, why = _run(["bootstrap", "gui/%d" % _uid(), plist], seams=seams)
    if why == "timeout":
        return "timeout", text
    if why:
        return "failed", text
    return ("ok" if code == 0 else "failed"), text


def bootout(bot_id, seams=None):
    """``bootout``（停用方向）。返回 ``(status, text)``，status ∈ ok / timeout / failed。

    「本来就没在跑」（``No such process`` / ``Could not find service``）等价成功 ——
    停用方向要的正是「它现在不在跑」这个终态，报错反而会让幂等的停用变成失败。
    """
    code, text, why = _run(["bootout", "gui/%d/%s" % (_uid(), label_of(bot_id))], seams=seams)
    if why == "timeout":
        return "timeout", text
    if why:
        return "failed", text
    if code == 0:
        return "ok", text
    if "No such process" in text or "Could not find service" in text:
        return "ok", text
    return "failed", text


# ---------------------------------------------------------------- print 解析与心跳

def parse_print(text):
    """解析 ``launchctl print`` 的输出，返回 ``(state, pid)``（都是 str 或 None）。

    只认行首**恰好一个**制表符的两行、各取第一条顶层匹配 —— 再往里的嵌套
    （缩进两层的子服务）与其它键一律不看。跟 autostart.ts 的 parseLaunchctlPrint 同款。
    """
    state = pid = None
    for m in _PRINT_RE.finditer(text or ""):
        key, value = m.group(1), m.group(2).strip()
        if key == "state" and state is None:
            state = value
        elif key == "pid" and pid is None:
            pid = value
    return state, pid


def print_state(bot_id, seams=None):
    """``launchctl print`` 的 ``(state, pid)``；失败或退出码非 0 按「没在运行」→ (None, None)。"""
    code, text, why = _run(["print", "gui/%d/%s" % (_uid(), label_of(bot_id))], seams=seams)
    if why or code != 0:
        return None, None
    state, raw_pid = parse_print(text)
    pid = int(raw_pid) if raw_pid and raw_pid.isdigit() else None
    return state, pid


def read_heartbeat(bot_id, seams=None):
    """心跳文件里的 ``{"at": 毫秒, "pid": int}``。

    读不出来（不存在 / 坏 JSON / 缺字段 / 类型不对）一律 None（I1-4）：判活只回答
    「有没有确凿的新鲜心跳」，坏文件不构成确凿证据，也不该把状态列拖成 500。
    """
    home = bot_home(bot_id, seams)
    if home is None:
        return None
    try:
        with open(home / "state" / "heartbeat", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    at, pid = data.get("at"), data.get("pid")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return {"at": at, "pid": pid}


def fresh_heartbeat(bot_id, now_ms=None, seams=None):
    """§2 状态列用的第 2、3 条：心跳新鲜回 ``{"at", "pid"}``，否则 None。不碰 launchctl。"""
    hb = read_heartbeat(bot_id, seams)
    if hb is None:
        return None
    now = time.time() * 1000 if now_ms is None else now_ms
    if now - hb["at"] > HEARTBEAT_FRESH_MS:
        return None
    return hb


def row_state(bot_id, seams=None):
    """状态列的一行判定（§2）：``(state, pid)``，state ∈ online / starting / offline。

    心跳新鲜 → online（pid 取心跳）；否则 ``print`` 说 running → starting（pid 取
    launchd 的）；再否则 offline。**不做 pid 一致性检查** —— 那是判活等待的事（§5 第 5 条），
    状态列只用第 2、3 条。
    """
    hb = fresh_heartbeat(bot_id, seams=seams)
    if hb is not None:
        return "online", hb["pid"]
    state, pid = print_state(bot_id, seams)
    if state == "running" and pid is not None:
        return "starting", pid
    return "offline", None


# ---------------------------------------------------------------- 判活（§5）

def alive_now(bot_id, since_ms=None, seams=None):
    """同一轮采样里判 §5 的五条。返回 ``(ok, reason, pid, age_s)``。

    - ``since_ms``：动作开始时刻（毫秒）。心跳的 ``at`` 早于它说明是**上一代进程**写的，
      不算数（重启场景传；状态列不传，走 ``row_state``）。
    - 成功时 pid 取 launchd 的、age_s 是心跳年龄；失败时 reason 是给人看的一句话。
    """
    now_ms = time.time() * 1000
    hb = read_heartbeat(bot_id, seams)
    if hb is None:
        return False, "没有新心跳", None, None
    if since_ms is not None and hb["at"] < since_ms:
        return False, "没有新心跳", None, None
    if now_ms - hb["at"] > HEARTBEAT_FRESH_MS:
        return False, "没有新心跳", None, None
    state, pid = print_state(bot_id, seams)
    if state is None:
        return False, "launchctl 没找到这个服务", None, None
    if state != "running" or pid is None:
        return False, "launchctl 里没在运行", None, None
    if hb["pid"] != pid:
        # I3：kickstart 的 SIGTERM 到旧进程退干净之间旧进程仍可能抢写一拍心跳
        return False, "心跳 pid 与 launchd 不一致", None, None
    return True, None, pid, max(0.0, (now_ms - hb["at"]) / 1000.0)


def _waited_reason(last, limit):
    """等待超时后的人话：具体原因（pid 不一致之类）留着，「没有新心跳」补上等了多久。"""
    if last == "没有新心跳":
        return "%g 秒内没有新心跳" % limit
    return last or "没有等到它活起来"


def wait_alive(bot_id, since_ms=None, timeout=None, seams=None):
    """轮询到 §5 全过或超时。返回 ``(ok, reason, pid, age_s)``。

    ``timeout`` 缺省读模块常量（重启 45 秒；新建流程显式传 30 秒）。超时按**最后一次
    采样的原因**报 —— 「pid 不一致」这类具体卡点比笼统的「超时」有用得多。
    """
    limit = RESTART_WAIT if timeout is None else timeout
    deadline = time.monotonic() + limit
    last = "没有新心跳"
    while True:
        ok, reason, pid, age = alive_now(bot_id, since_ms, seams)
        if ok:
            return True, None, pid, age
        last = reason or last
        if time.monotonic() >= deadline:
            return False, _waited_reason(last, limit), None, None
        time.sleep(POLL_INTERVAL)


def enabled_of(bot_id):
    """新系统这边**该不该跑**这只 bot（``enabled`` 的判定口径在 ``bots_registry.is_enabled``）。

    读的是新系统那份配置：这只生效的那份不是新系统的（没有这只，或生效的是旧系统那份），
    一律回否 —— 新系统这边就不该跑它（§5b 第 4 条）。过放行闸进来的重启不受影响；它只在
    开关「新旧同名、停用之后生效的变成旧系统那份」时起作用：那次同步必须走停用方向，
    否则会把刚停的 bot 反向拉起，新旧两个进程同 token 双跑。

    每次调用现读、不缓存。会读配置文件，所以只许在处理请求的线程里调（§1a）：
    job 的方向由 ``one_runner`` / ``all_runner`` 在受理时读好，后台不再调它。
    """
    from moments import config_sources
    cfg = config_sources.effective_config(bot_id)
    if not cfg or cfg.get("_source") != config_sources.SOURCE_DSH:
        return False
    from bots_registry import is_enabled
    return bool(is_enabled(cfg))


# ---------------------------------------------------------------- 单只执行体（§4 / §4b）

def _launchctl_timeout_reason():
    return "launchctl 超时（%g 秒）" % LAUNCHCTL_TIMEOUT


def _start_one(bot_id, seams=None):
    """启用方向：kickstart（找不到服务时回落 bootstrap）。返回 ``(ok, reason)``。"""
    status, _text = kickstart(bot_id, seams)
    if status == "timeout":
        return False, _launchctl_timeout_reason()
    if status == "missing":
        bstatus, _t2 = bootstrap(bot_id, seams)
        if bstatus == "no_plist":
            return False, "单元没装（plist 不存在）"
        if bstatus == "timeout":
            return False, _launchctl_timeout_reason()
        if bstatus != "ok":
            return False, "launchctl bootstrap 失败"
        return True, None
    if status != "ok":
        return False, "launchctl kickstart 失败"
    return True, None


def _stop_one(bot_id, seams=None):
    """停用方向：bootout（容错语义见 ``bootout``）。返回 ``(ok, reason)``。"""
    status, _text = bootout(bot_id, seams)
    if status == "timeout":
        return False, _launchctl_timeout_reason()
    if status != "ok":
        return False, "launchctl bootout 失败"
    return True, None


# ---------------------------------------------------------------- tail 规约（§8）

def _summary(total, ok_n):
    if total <= 0:
        return "没有可重启的 bot"
    if ok_n >= total:
        return "重启 %d 只：成功 %d" % (total, ok_n)
    return "重启 %d 只：成功 %d，失败 %d" % (total, ok_n, total - ok_n)


def _ok_line(bot_id, stopped, pid=None, age=None):
    if stopped:
        return "%s 已停止" % bot_id
    return "%s 已重启（pid %s，心跳 %d 秒前）" % (bot_id, pid, int(round(age or 0)))


def _fail_line(bot_id, reason, seams=None):
    return "%s 失败：%s；日志 %s" % (bot_id, reason or "没起来", log_path(bot_id, seams))


def _join(summary, lines):
    return "\n".join([summary] + [ln for ln in lines if ln])


# ---------------------------------------------------------------- 编组

def sync_one(bot_id, enabled=None, seams=None):
    """**单只同步**的 job 执行体，返回 ``(state, exit_code, tail)``。

    方向 = ``enabled``：启用 → kickstart（回落 bootstrap）再等 §5 存活（上限 45 秒）；
    停用 → bootout，不等心跳、命令成功即 done。exit_code 恒 None（launchd 路径没有
    「命令退出码」这回事，不编一个）。

    ``enabled`` 与 ``seams`` 由 ``one_runner`` 在受理时定好再传进来（§1a），后台不读配置、
    不读环境。两个都缺省时现读（``enabled_of`` 与环境），留给直接调用它的地方。
    """
    if enabled is None:
        enabled = enabled_of(bot_id)
    if enabled:
        started = time.time() * 1000        # 判活的起始时刻 = 动作开始时刻（§5 第 2 条）
        ok, reason = _start_one(bot_id, seams)
        if ok:
            alive, why, pid, age = wait_alive(bot_id, since_ms=started, seams=seams)
            if alive:
                return "done", None, _join(_summary(1, 1), [_ok_line(bot_id, False, pid, age)])
            reason = why
        return "failed", None, _join(_summary(1, 0), [_fail_line(bot_id, reason, seams)])
    ok, reason = _stop_one(bot_id, seams)
    if ok:
        return "done", None, _join(_summary(1, 1), [_ok_line(bot_id, True)])
    return "failed", None, _join(_summary(1, 0), [_fail_line(bot_id, reason, seams)])


def restart_group(pairs, seams=None):
    """**多只编组**的 job 执行体（§3）：``pairs = [(bot_id, enabled)]``。

    命令串行发（每只一条 launchctl），发完对启用中那几只**并行**等活（上限 45 秒），
    总耗时远小于 job 的兜底上限。返回 ``(state, exit_code, tail)``，exit_code 恒 None。
    ``seams`` 一路传到底，并行等活的子线程也只用它（§1a）。
    """
    if not pairs:
        return "done", None, _summary(0, 0)
    pending = []       # [(bot_id, started_ms)]：命令已发、等 §5 存活
    final = {}         # bot_id -> (ok, pid, age, reason)
    for bot_id, enabled in pairs:
        if not enabled:
            ok, reason = _stop_one(bot_id, seams)
            final[bot_id] = (ok, None, None, reason)
            continue
        started = time.time() * 1000
        ok, reason = _start_one(bot_id, seams)
        if ok:
            pending.append((bot_id, started))
        else:
            final[bot_id] = (False, None, None, reason)
    if pending:
        with ThreadPoolExecutor(max_workers=min(MAX_WAIT_WORKERS, len(pending))) as ex:
            futs = {bot_id: ex.submit(wait_alive, bot_id, since, None, seams)
                    for bot_id, since in pending}
            for bot_id, since in pending:
                ok, reason, pid, age = futs[bot_id].result()
                final[bot_id] = (ok, pid, age, reason)
    lines, ok_n = [], 0
    for bot_id, enabled in pairs:
        ok, pid, age, reason = final.get(bot_id, (False, None, None, "没有结果"))
        if ok:
            ok_n += 1
            lines.append(_ok_line(bot_id, not enabled, pid, age))
        else:
            lines.append(_fail_line(bot_id, reason, seams))
    state = "done" if ok_n >= len(pairs) else "failed"
    return state, None, _join(_summary(len(pairs), ok_n), lines)


# ---------------------------------------------------------------- job 执行体的工厂（§1a）

def _failed_runner(exc):
    """受理时算范围 / 读方向就抛了：job 照样受理，直接以 failed 结束，tail 只给类名。

    与原先「后台线程里抛出、由 ``RestartJobs`` 兜成 failed」的对外行为相同 ——
    不让这次请求变成 500（开关那条路上写盘已经发生了）。
    """
    name = type(exc).__name__

    def runner():
        return "failed", None, name
    return runner


def one_runner(bot_id, enabled=None):
    """单只同步的 job 执行体（无参 callable，给 ``RestartJobs.start`` 用）。

    **在处理请求的线程里调**（§1a）：接缝与方向在这里定下，返回的闭包只拿着算好的值
    执行，后台不再读环境与配置。``enabled`` 是调用方已知的方向（行内重启进来时必为
    启用）；不给就在这里读一次 ``enabled_of`` —— 开关同步走这条，读到的是写盘后的值。
    """
    seams = resolve_seams()
    try:
        direction = enabled_of(bot_id) if enabled is None else bool(enabled)
    except Exception as e:                  # noqa: BLE001  翻成 failed 的 job，见 _failed_runner
        return _failed_runner(e)

    def runner():
        return sync_one(bot_id, direction, seams)
    return runner


def restart_pairs():
    """「重启全部」的执行范围与每只的方向（§3）：``[(bot_id, enabled)]``。

    范围 = ``hub_visible(list_bots(), bot_sources())`` 的结果里**过放行闸**的那些
    （``strict``：来源读不出、名单注入、新系统根不在场时为空，一条 launchctl 都不发）；
    方向逐只取 ``enabled_of``。会读配置，只许在处理请求的线程里调。
    """
    from moments import bots_client     # 延迟 import：与 bots_client 双向依赖，函数内解
    entries = bots_client.hub_visible(bots_client.list_bots(), bots_client.bot_sources(),
                                      strict=True)
    return [(e["id"], enabled_of(e["id"])) for e in entries]


def restart_all():
    """「重启全部」的同步入口：现算范围与方向、现读接缝，直接执行。

    走 job 的那条路用 ``all_runner``（受理时定下再交给后台）；这个入口留给直接调用它的地方。
    """
    return restart_group(restart_pairs())


def all_runner():
    """「重启全部」的 job 执行体（无参 callable，给 ``RestartJobs.start`` 用）。

    **在处理请求的线程里调**（§1a）：接缝、执行范围、每只的方向都在这里定下，返回的闭包
    只调 ``restart_group``。受理之后配置与环境再怎么改，这次 job 仍按受理时的样子做完。
    """
    seams = resolve_seams()
    try:
        pairs = restart_pairs()
    except Exception as e:                  # noqa: BLE001  名单读不出来：见 _failed_runner
        return _failed_runner(e)

    def runner():
        return restart_group(pairs, seams)
    return runner
