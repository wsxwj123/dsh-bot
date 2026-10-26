# -*- coding: utf-8 -*-
"""provider-hub 二期验收夹具（黑盒）。

只依据 `.devflow/BRIEF-hub2.md` 与 `INTERFACE-hub2.md` 编写，不读实现代码。
实现未落地时本目录整体红 —— 那是红基线，不是夹具坏了。

硬隔离（§1 测试接缝 + BRIEF 敏感面）：
1. `HUB_CONFIGS_DIR` 指 tmp；autouse 二次断言它不等于仓库 `configs/`。
2. `HUB_TELEGRAM_API_BASE` autouse 强制指向 127.0.0.1（未设或指向真实域名 → 该用例直接 fail），
   任何用例都不可能打到 api.telegram.org。
3. `HUB_LAUNCHAGENTS_DIR` 指 tmp；`CLAUDE_SETTINGS_PATH` / `HUB_ENV_FILE` 沿一期做法钉在 tmp。
4. **`HUB_CHANNELS_DIR` 指 tmp**（§13 A2 新增接缝）：频道根、模板扫描、A7 的 `.env` 比对全经它。
   `HOME` 仍钉在 tmp，但**它不再是接缝、只是防炸垫**：实现若无视 `HUB_CHANNELS_DIR`，
   产物会落进 fakehome 而不是用户真实 `~/.claude/channels/`（那里跑着生产 bot），
   用例照样红在「产物不在 `HUB_CHANNELS_DIR` 下」这条断言上。
5. **新契约的落点与判活面另有两个接缝，autouse 钉在 tmp**：`HUB_CONFIGS_DSH_DIR`（`<bot>.yml`
   的目标根）与 `DSH_BOT_HOME`（`bots/<bot>/...` 的家目录）。不钉住就等于拿真机 `~/.dsh-bot`
   去建 bot、去读写跑着的生产 bot 的家 —— `_no_touch_real` 逐个反向断言。
6. **两个执行接缝 autouse 指毒丸**：`HUB_BUN` / `HUB_LAUNCHCTL`（§1）。谁真去跑 autostart.ts、
   谁真调 launchctl，谁当场非 0，测试进程绝不碰 gui domain 与真实 `~/Library/LaunchAgents`。
   要「活条件齐」的用例自己经 `fake_bun` / `fake_launchctl` / `alive_bot` 三件套注入。
7. **`HUB_RESTART_SCRIPT` 已退役（§1 末行）**：实现与测试都不再读它，夹具也不再提供替身。
8. 不监听生产端口、不起真 bot、不调 launchctl；假 token 形如 `123456789:test-...`。
"""


def _require_total_gate():
    """自检：tests/conftest.py 的总闸没加载，就拒绝运行本目录的用例。

    pytest 只加载「启动目录及其下」的 conftest。cd 到本目录（或它的某个上级子目录）里面
    再起 pytest，总闸不会被加载：HUB_LAUNCHCTL / HUB_BUN 不是毒丸，用例可能碰到真实的
    launchd。总闸导入时在 sys 上留了带本进程 pid 的标记（tests/conftest.py 的
    _STATE_ATTR），这里只认它。自测在 tests/gate_selftest/。
    """
    import os
    import sys
    gate = getattr(sys, "_dsh_bot_test_gate", None)
    if isinstance(gate, dict) and gate.get("pid") == os.getpid():
        return
    here = os.path.dirname(os.path.abspath(__file__))
    tests = here
    while os.path.basename(tests) != "tests" and os.path.dirname(tests) != tests:
        tests = os.path.dirname(tests)
    root = os.path.dirname(tests)
    raise RuntimeError(
        "测试总闸（tests/conftest.py）没有加载，拒绝运行本目录的用例。\n"
        "在子目录里面启动 pytest 时总闸不生效，用例可能碰到真实的 launchd。\n"
        "请从仓库根或 tests/ 目录启动，例如：\n"
        "    cd %s && python3 -m pytest %s" % (root, os.path.relpath(here, root)))


_require_total_gate()
del _require_total_gate

import importlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = _ROOT
REAL_HOME = Path(os.path.expanduser("~"))          # 在 monkeypatch HOME 之前定住
REAL_CONFIGS = REPO_ROOT / "configs"
REAL_CHANNELS = REAL_HOME / ".claude" / "channels"
REAL_SETTINGS = REAL_HOME / ".claude" / "settings.json"
REAL_DSH = REAL_HOME / ".dsh-bot"                  # 新系统家目录（跑着生产 bot）

FAKE_TOKEN = "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11"
FAKE_TOKEN_2 = "123456780:test-ZZzzYYyyXXxxWWwwVVvvUUuuTTss22"

import spec  # noqa: E402
from stub_telegram import StubTelegram  # noqa: E402


def _load_app():
    """重新导入门户 Flask app（env 在导入期被读，改 env 必须重载）。照抄一期做法。"""
    mod = sys.modules.get("moments.web")
    mod = importlib.reload(mod) if mod else importlib.import_module("moments.web")
    for attr in ("app", "application"):
        obj = getattr(mod, attr, None)
        if obj is not None and hasattr(obj, "test_client"):
            return obj
    factory = getattr(mod, "create_app", None)
    if callable(factory):
        return factory()
    raise AssertionError("moments.web 未导出可用的 Flask app（试过 app/application/create_app()）")


def free_port():
    """拿一个当前空闲的端口号并立刻释放（用于"没人监听"的前提）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _snapshot(d):
    d = Path(d)
    if not d.exists():
        return None
    out = {}
    for p in sorted(d.iterdir()):
        try:
            out[p.name] = (p.is_dir(), p.stat().st_mtime_ns, p.stat().st_size)
        except OSError:
            out[p.name] = ("gone",)
    return out


@pytest.fixture
def configs_dir(tmp_path):
    """§7 `HUB_CONFIGS_DIR`：`_global.yml` / `<bot>.yml` / 备份全在其下。"""
    d = tmp_path / "configs"
    d.mkdir()
    assert d.resolve() != REAL_CONFIGS.resolve(), "HUB_CONFIGS_DIR 指到了真实 configs/"
    return d


@pytest.fixture
def global_yml(configs_dir):
    """写好样本 `_global.yml`（含注释/锚点/merge key/块标量/敏感 key），返回路径。"""
    p = configs_dir / "_global.yml"
    p.write_text(spec.global_yml(), encoding="utf-8")
    os.chmod(p, 0o600)
    return p


@pytest.fixture
def launchagents_dir(tmp_path):
    """§7 `HUB_LAUNCHAGENTS_DIR`：设为非默认值时不许实调 launchctl。"""
    d = tmp_path / "LaunchAgents"
    d.mkdir()
    return d


@pytest.fixture
def telegram():
    """§7 冻结的 getMe 桩，绑 127.0.0.1 随机端口。"""
    s = StubTelegram().start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch, configs_dir, launchagents_dir, channels_dir,
             dsh_home, dsh_configs_dir, poison):
    """autouse：任何用例都不许继承外部真实口令/家目录/配置目录，也不许碰到真机的
    新系统家目录与那两个执行接缝（见模块 docstring 5/6/7）。"""
    for k in ("HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD", "HUB_RESTART_CMD",
              "MOMENTS_WEB_PORT", "ACCESS_PASSWORD", "CLIPROXY_MGMT_KEY",
              "HUB_RESTART_SCRIPT"):
        monkeypatch.delenv(k, raising=False)
    fake_home = tmp_path / "fakehome"
    (fake_home / ".claude" / "channels").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))   # 防炸垫，不是接缝（见模块 docstring 4）
    monkeypatch.setenv("USERPROFILE", str(fake_home))   # Windows 上 expanduser 读它
    monkeypatch.setenv("CLAUDE_TGBOT_HOME", str(tmp_path / "tgbot-home"))
    (tmp_path / "tgbot-home").mkdir(exist_ok=True)
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(fake_home / ".claude" / "settings.json"))
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "hub.env"))
    monkeypatch.setenv("MOMENTS_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs_dir))
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(launchagents_dir))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(channels_dir))          # §13 A2
    # 新契约的落点与判活面（§1）：不钉住 = 拿真机 ~/.dsh-bot 建 bot
    monkeypatch.setenv("DSH_BOT_HOME", str(dsh_home))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh_configs_dir))
    # 两个执行接缝默认毒丸：真要跑 autostart.ts / 真调 launchctl 的路径当场非 0
    monkeypatch.setenv("HUB_BUN", str(poison / "bun"))
    monkeypatch.setenv("HUB_LAUNCHCTL", str(poison / "launchctl"))
    # §7：未设 base 即 fail 该用例。默认指一个没人监听的回环端口（= network 分型）。
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", f"http://127.0.0.1:{free_port()}")
    base = os.environ.get("HUB_TELEGRAM_API_BASE", "")
    assert base, "HUB_TELEGRAM_API_BASE 未设 → 直接判该用例失败（§7）"
    assert "127.0.0.1" in base or "localhost" in base, f"getMe base 不是回环：{base}"


def _names(d):
    """只取顶层条目名（+是否目录）。用于本机跑着生产 bot 的目录：
    它们的 mtime 会被真 bot 自己的写入不断刷新，拿 mtime 比对必然假红。
    接缝漏了的特征是**冒出/消失一个条目**（如 `channels/xiaoyu/`），名字集合就能抓住。"""
    d = Path(d)
    if not d.exists():
        return None
    return {p.name: p.is_dir() for p in d.iterdir()}


@pytest.fixture(autouse=True)
def _no_touch_real():
    """反向断言：跑完任何用例后，真实 configs/、真实 ~/.claude/channels、
    真实 settings.json、真实 LaunchAgents、真实 ~/.dsh-bot 必须一字未动
    （接缝漏了就在这里炸）。"""
    real_la = REAL_HOME / "Library" / "LaunchAgents"
    before = (_snapshot(REAL_CONFIGS), _names(REAL_CHANNELS), _snapshot(real_la),
              REAL_SETTINGS.stat().st_mtime_ns if REAL_SETTINGS.exists() else None,
              _names(REAL_DSH / "bots"), _snapshot(REAL_DSH / "configs"))
    yield
    after = (_snapshot(REAL_CONFIGS), _names(REAL_CHANNELS), _snapshot(real_la),
             REAL_SETTINGS.stat().st_mtime_ns if REAL_SETTINGS.exists() else None,
             _names(REAL_DSH / "bots"), _snapshot(REAL_DSH / "configs"))
    assert before[0] == after[0], "用例改动了仓库真实 configs/"
    assert before[1] == after[1], "用例在真实 ~/.claude/channels/ 下增删了条目"
    assert before[2] == after[2], "用例改动了真实 ~/Library/LaunchAgents/"
    assert before[3] == after[3], "用例改动了真实 ~/.claude/settings.json"
    assert before[4] == after[4], "用例在真实 ~/.dsh-bot/bots/ 下增删了条目（新系统的家目录）"
    assert before[5] == after[5], "用例改动了真实 ~/.dsh-bot/configs/（那里是跑着的 bot）"


@pytest.fixture
def make_client(monkeypatch, configs_dir, launchagents_dir):
    """(**env) -> (client, app)。按给定 env 重载门户拿测试客户端。"""
    def _make(**env):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, str(v))
        app = _load_app()
        app.config["TESTING"] = True
        return app.test_client(), app
    return _make


class _Api:
    """(code, body) 小工具；body 解不出 JSON 时给 {"_raw_text", "_ctype"}。"""

    def __init__(self, client):
        self.c = client

    @staticmethod
    def _wrap(r):
        try:
            j = r.get_json()
        except Exception:
            j = None
        if j is None:
            return r.status_code, {"_raw_text": r.get_data(as_text=True)[:2000],
                                   "_ctype": r.headers.get("Content-Type", "")}
        return r.status_code, j

    def get(self, path, **kw):
        return self._wrap(self.c.get(path, **kw))

    def post(self, path, payload=None, **kw):
        if payload is not None and "data" not in kw:
            kw["json"] = payload
        elif payload is None and "data" not in kw and "json" not in kw:
            kw["json"] = {}
        return self._wrap(self.c.post(path, **kw))

    def raw_get(self, path, **kw):
        return self.c.get(path, **kw)


@pytest.fixture
def client(make_client):
    """默认客户端：门关闭（一期老用户形态），可直达 /hub*。"""
    c, _ = make_client()
    return c


@pytest.fixture
def api(client):
    return _Api(client)


@pytest.fixture
def apif(make_client):
    """需要自定义 env 时用：apif(HUB_TELEGRAM_API_BASE=...) -> _Api。"""
    def _f(**env):
        c, app = make_client(**env)
        a = _Api(c)
        a.app = app          # 并发用例要从同一个 app 再开一个 client（共享进程内锁）
        return a
    return _f


@pytest.fixture(autouse=True)
def _safe_restart_cmd(monkeypatch, _isolate):
    """默认把三个重启触发口的覆盖命令钉在 no-op 上（`HUB_RESTART_CMD`）——
    不设的话真机会去调 launchd。它不参与新建流程的 restart 步（§6：那步只看 §5 判活）。

    原来那句注释里的 restart-bots.sh 已退役（§1 末行），这里只留覆盖命令这一层。"""
    monkeypatch.setenv("HUB_RESTART_CMD", "/usr/bin/true")


@pytest.fixture
def held_port():
    """占住一个端口不放，用于 §4.2「该端口本机已被监听 → 409 port_in_use」。"""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    port = s.getsockname()[1]
    try:
        yield port
    finally:
        s.close()


def version_of(path):
    """§2.1 定义：`f"{st_mtime_ns}-{st_size}"`；文件不存在时 `"absent"`。"""
    p = Path(path)
    if not p.exists():
        return "absent"
    st = p.stat()
    return f"{st.st_mtime_ns}-{st.st_size}"


def login(client, password=None, admin_password=None):
    form = {}
    if password is not None:
        form["password"] = password
    if admin_password is not None:
        form["admin_password"] = admin_password
    return client.post("/login", data=form,
                       content_type="application/x-www-form-urlencoded")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 含真实等待（10s getMe 超时 / 30s 探活）")
    config.addinivalue_line("markers", "concurrency: 起线程，可能受调度影响")


@pytest.fixture
def as_api():
    """把任意 test_client 包成 (code, body) 形式（避免用例去 import conftest）。"""
    return _Api


@pytest.fixture
def do_login():
    return login


@pytest.fixture
def channels_dir(tmp_path):
    """§7 `HUB_CHANNELS_DIR`（§13 A2 新增接缝）：频道根。
    B 段产物树、§4.4 模板扫描、A7 的 `.env` 比对都必须落在这里 —— 落到别处即红。"""
    d = tmp_path / "channels"
    d.mkdir(parents=True, exist_ok=True)
    assert d.resolve() != REAL_CHANNELS.resolve(), "HUB_CHANNELS_DIR 指到了真实 channels/"
    return d


@pytest.fixture
def persona_template(channels_dir):
    """造一个可选的人设模板：`channels/<id>/CLAUDE.md`（§4.2 的可选值来源）。"""
    d = channels_dir / "chenlulu"
    d.mkdir(parents=True, exist_ok=True)
    (d / "CLAUDE.md").write_text("我是陈璐璐模板正文。\n", encoding="utf-8")
    return "chenlulu"


@pytest.fixture
def existing_bot(configs_dir, channels_dir):
    """预置一个"已存在的 bot"：configs/<id>.yml + channels/<id>/。"""
    def _make(bot_id="chenlulu", port=17801, chat_id=111):
        (configs_dir / f"{bot_id}.yml").write_text(
            f"id: {bot_id}\ndisplay_name: 陈璐璐\n"
            f"bot_channel_path: ~/.claude/channels/{bot_id}\n"
            f"chat_id: {chat_id}\ndispatcher_port: {port}\n"
            f"namespace_uuid: 11111111-2222-3333-4444-555555555555\n",
            encoding="utf-8")
        d = channels_dir / bot_id
        d.mkdir(parents=True, exist_ok=True)
        return bot_id
    return _make


@pytest.fixture
def port():
    """一个当前空闲的端口号（没人监听），供加 bot 用例做 dispatcher_port。"""
    return free_port()


@pytest.fixture
def port2():
    return free_port()


# ---------------------------------------------------------------- 新系统那套面的夹具
# 新建 bot 落新系统（§6/§7）：配置进 `HUB_CONFIGS_DSH_DIR`、频道树进
# `<DSH_BOT_HOME>/bots/<bot>/channel`；判活看心跳与 launchd（§5）。
# 四个夹具按「隔离 → 执行替身 → 活证据」分工，用例只拿自己要的那几件。

@pytest.fixture
def dsh_home(tmp_path):
    """§1 `DSH_BOT_HOME`：新系统家目录（与 `chat_history`、网关同名同义）。"""
    d = tmp_path / "dsh-home"
    d.mkdir()
    assert d.resolve() != REAL_DSH.resolve(), "DSH_BOT_HOME 指到了真实 ~/.dsh-bot"
    return d


@pytest.fixture
def dsh_configs_dir(tmp_path):
    """§1 `HUB_CONFIGS_DSH_DIR`：新系统配置根，新建 bot 的 `<bot>.yml` 落这里。"""
    d = tmp_path / "dsh-configs"
    d.mkdir()
    return d


@pytest.fixture
def poison(tmp_path):
    """两个执行接缝的毒丸（§1 `HUB_BUN` / `HUB_LAUNCHCTL`）：谁真去跑 autostart.ts、
    谁真调 launchctl，谁当场非 0 —— 测试绝不碰真实 LaunchAgents 与 gui domain。"""
    d = tmp_path / "poison"
    d.mkdir()
    for name in ("bun", "launchctl"):
        p = d / name
        p.write_text(f"#!/bin/sh\necho 'poison {name} called' >&2\nexit 97\n", encoding="utf-8")
        p.chmod(0o755)
    return d


class _FakeBun:
    """假 bun 的一份实例：`path` 喂给 `HUB_BUN`，`calls` 是已发生的调用记录。"""

    def __init__(self, path, log):
        self.path = Path(path)
        self._log = Path(log)

    @property
    def calls(self):
        """[{"argv": [脚本, 子命令, ...], "path": 子进程的 PATH}]，按发生顺序。

        `argv` 不含可执行文件本身（它是 shell 的 `$0`），第一项是 autostart.ts 的路径。
        """
        out = []
        for line in self._log.read_text(encoding="utf-8").splitlines():
            argv, _, env_path = line.partition("\t")
            out.append({"argv": argv.split(), "path": env_path})
        return out


#: §5 的活证据里那个 pid：假 launchd 报的与心跳写的必须是同一个（I3）。
ALIVE_PID = 4242


def _sh_quote(path):
    """单引号包裹路径（夹具里的路径没有单引号，够用）。"""
    return "'" + str(path).replace("'", "'\\''") + "'"


@pytest.fixture
def fake_bun(tmp_path):
    """`(fail_on=None) -> _FakeBun`：假 bun（§1 `HUB_BUN`），绝不真跑 autostart.ts。

    `fail_on` ∈ {None, "install", "install-jobs"}：该条子命令退非 0，用来构造 §6 的
    短路与分型。每次调用把 argv 与子进程 PATH 记进一份 log（同一 `fail_on` 复用同一份）。
    """
    made = {}

    def _make(fail_on=None):
        if fail_on not in made:
            tag = fail_on or "ok"
            exe = tmp_path / f"fake-bun-{tag}"
            log = tmp_path / f"bun-calls-{tag}.jsonl"
            log.write_text("", encoding="utf-8")
            exe.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\t%s\\n' \"$*\" \"$PATH\" >> {_sh_quote(log)}\n"
                # $1 是 autostart.ts 的路径，$2 才是子命令（install / install-jobs）
                f"if [ \"$2\" = \"{fail_on or '__never__'}\" ]; then\n"
                "  echo '⚠️ fake bun 的原始输出（不该进响应）'\n"
                "  exit 7\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8")
            exe.chmod(0o755)
            made[fail_on] = _FakeBun(exe, log)
        return made[fail_on]
    return _make


@pytest.fixture
def fake_launchctl(tmp_path):
    """`(kind="running") -> 假 launchctl 的可执行文件路径`（§1 `HUB_LAUNCHCTL`）。

    running：`print` 报 `state = running` + `pid`（与 `alive_bot` 写的心跳一致），退 0
    ——§5 第 4 条的证据；stopped：`print` 非 0 退出、什么都不报（按「没在运行」处理）。
    """
    made = {}
    bodies = {
        "running": ("#!/bin/sh\n"
                    "if [ \"$1\" = \"print\" ]; then\n"
                    "  printf '\\tgui/501/com.dsh-bot.x = {\\n'\n"
                    "  printf '\\tstate = running\\n'\n"
                    "  printf '\\tpid = " + str(ALIVE_PID) + "\\n'\n"
                    "  printf '}\\n'\n"
                    "fi\n"
                    "exit 0\n"),
        "stopped": ("#!/bin/sh\n"
                    "if [ \"$1\" = \"print\" ]; then\n"
                    "  echo 'Could not find service in domain for user gui: 501' >&2\n"
                    "  exit 113\n"
                    "fi\n"
                    "exit 0\n"),
    }

    def _make(kind="running"):
        if kind not in made:
            p = tmp_path / f"fake-launchctl-{kind}"
            p.write_text(bodies[kind], encoding="utf-8")
            p.chmod(0o755)
            made[kind] = p
        return made[kind]
    return _make


@pytest.fixture
def alive_bot(dsh_home):
    """`(bot_id) -> None`：让这只 bot「活」—— 后台线程持续写 §5 第 1 条要的心跳，
    `pid` = 假 launchd print 报的那个，`at` 每 0.2 秒刷新（永远新鲜，§5 第 3 条）。

    只在 `<bot 家>` 出现之后才写：A 段把「家目录已存在」判成占用（409），
    测试替面板建目录会把好用例判死；等它出现也顺带保证 `at` 不早于动作开始时刻（§5 第 2 条）。
    """
    stops = []

    def _start(bot_id):
        home = Path(dsh_home) / "bots" / bot_id
        state = home / "state"
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                if home.is_dir():
                    state.mkdir(exist_ok=True)
                    (state / "heartbeat").write_text(
                        json.dumps({"at": int(time.time() * 1000), "pid": ALIVE_PID}),
                        encoding="utf-8")
                stop.wait(0.2)

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        stops.append((stop, t))

    yield _start
    for stop, t in stops:
        stop.set()
        t.join(timeout=5)
