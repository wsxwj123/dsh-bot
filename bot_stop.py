"""需求⑤ 单 bot 停止：Python 侧权威判定（INTERFACE §9.1）+ 测试模式自检（§11.2/§11.3）。

真相：<DISABLED_DIR>/<bot> 存在 = 已停止（stop-bot.sh 写、start-bot.sh 删）。shell 侧同一真相由
dispatcher 仓 bot-enabled.sh 的 bot_stopped 读，restart-bots.sh 的 enabled() 是第三个读者；三者默认路径必须一致。
调用方：director.py、moments/web.py、moments/post.py、voicecall-demo/server.py——写 inbox / 拉起 worker 之前问一次。
标准库 only；任何函数都不抛（判定失败一律当"未停止"，即改动前的旧行为）。
"""
import glob
import os
import re
import sys

DISABLED_DIR = os.path.expanduser(os.environ.get("DIRECTOR_DISABLED_DIR") or "~/.claude/dispatcher/.disabled")

_warned_unreadable = False


def is_stopped(bot) -> bool:
    """bot 是否被手动停止。bot 非 str / 空 / 含 "/" / 为 "." ".." → False（防拼出目录本身或越界路径）。"""
    global _warned_unreadable
    if not isinstance(bot, str) or "/" in bot or bot in ("", ".", ".."):
        return False
    try:
        os.stat(os.path.join(DISABLED_DIR, bot))
        return True
    except (FileNotFoundError, NotADirectoryError, ValueError):  # 无标记 / 目录缺 / 名字含 NUL
        return False
    except OSError as e:  # 目录不可读：fail-open 当未停止，整个进程只报一次
        if not _warned_unreadable:
            _warned_unreadable = True
            sys.stderr.write(f"bot_stop: unreadable {type(e).__name__}\n")
        return False


def stopped_bots(bots) -> set:
    """对可迭代（list/set/dict 键/生成器）逐个 is_stopped，返回已停止的集合；非字符串项忽略。"""
    try:
        return {b for b in bots if is_stopped(b)}
    except TypeError:  # bots 不可迭代
        return set()


# ══════════════════ 测试模式自检（CLAUDEBOTLIFE_TEST=1 时 fail-closed）══════════════════
_FORBIDDEN_PORTS = {17801, 17802, 17803, 17804, 7897, 7788}  # 生产 dispatcher / 代理 / 语音桥
# 会调外部命令的入口模块（voicecall 在测试里以 vcserver 名加载，生产以 server 名被 uvicorn 加载）
_PRODUCERS = ("director", "moments.web", "moments.post", "vcserver", "server")


def _refuse(token: str):
    raise RuntimeError(f"test_mode: {token}")


def _under(path: str, root: str) -> bool:
    """字符串规范化（去 ..、去尾斜杠）后等于 root 或以 root/ 开头；不解析符号链接（§11.2 第 1 条）。"""
    p = os.path.normpath(path)
    return p == root or p.startswith(root + os.sep)


def _injected_ports(mods: list) -> set:
    """注入的端口（§11.2 第 3 条）：director.BOT_PORTS 被改过的值 + 注入配置目录里 yml 的 dispatcher_port。
    BOT_PORTS 的出厂默认值不算注入：director 从不直连它，只作为参数交给（测试里必被替换的）_subprocess。"""
    # `_DEFAULT_BOT_PORTS` 是旧系统 director 模块的内部属性名（判据取自它，不改名）；
    # 新系统（dsh-bot）的 bot 端口表权威来源是 `bots_registry`（扫 configs/*.yml，加 bot 零代码改动），
    # 本函数只在测试态比对"端口是否被注入"，不解析 bot 端口表，故不 import 它。
    vals = []
    d = sys.modules.get("director")
    if d in mods and getattr(d, "BOT_PORTS", None) != getattr(d, "_DEFAULT_BOT_PORTS", None):
        vals += list((getattr(d, "BOT_PORTS", None) or {}).values())
    for var in ("CLAUDEBOT_CONFIG_DIR", "HUB_CONFIGS_DIR"):
        if not os.environ.get(var):
            continue
        for path in glob.glob(os.path.join(os.environ[var], "*.yml")):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    vals += re.findall(r"^\s*dispatcher_port\s*:\s*['\"]?(\d+)", f.read(), re.M)
            except OSError:
                continue
    ports = set()
    for v in vals:
        try:
            ports.add(int(v))
        except (TypeError, ValueError):
            pass
    return ports


def test_mode_selfcheck() -> None:
    """CLAUDEBOTLIFE_TEST=1 时逐条校验隔离注入，按序首个不满足 → RuntimeError("test_mode: <token>")；
    开关未设或不为 "1" → 空操作（生产零开销）。token 顺序即契约（§11.2 第 5 条）。"""
    env = os.environ
    if env.get("CLAUDEBOTLIFE_TEST") != "1":
        return
    root = env.get("CLAUDEBOTLIFE_TEST_ROOT") or ""
    if not os.path.isabs(root) or not os.path.isdir(root):
        _refuse("missing_root")
    root = os.path.normpath(root)
    if not _under(env.get("HOME") or "", root):
        _refuse("home_outside_root")
    dir_vars = ["DIRECTOR_CHANNELS_ROOT", *sorted(k for k in env if k.startswith("DIRECTOR_") and k.endswith("_DIR")),
                "CLAUDEBOT_CONFIG_DIR", "HUB_CONFIGS_DIR"]
    for var in dir_vars:
        if env.get(var) and not _under(env[var], root):
            _refuse(f"path_outside_root:{var}")
    mods = [m for m in (sys.modules.get(n) for n in _PRODUCERS) if m is not None]
    subs = [m._subprocess for m in mods if hasattr(m, "_subprocess")]
    # 没有任何入口模块换过 _subprocess（含一个都没加载）也算"未换"
    if not env.get("DIRECTOR_NO_SPAWN") and (not subs or any(s is sys.modules.get("subprocess") for s in subs)):
        _refuse("real_subprocess")
    ur = sys.modules.get("urllib.request")
    if ur is not None and any(getattr(m, "_urlopen", None) is ur.urlopen for m in mods):
        _refuse("real_urlopen")
    if _injected_ports(mods) & _FORBIDDEN_PORTS:
        _refuse("forbidden_port")
