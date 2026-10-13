# -*- coding: utf-8 -*-
"""「旧系统供应商」只留入口这一批的验收夹具（黑盒）。

只依据 `.devflow/BRIEF-旧系统供应商入口.md` 与 `.devflow/INTERFACE-旧系统供应商入口.md`，
不读实现代码。实现未落地时量新行为的用例是红的，那是红基线，不是夹具坏了。

硬隔离
------
1. 家目录、新系统家目录、两个配置根、频道根、启动项目录、朋友圈库、settings.json、hub.env
   全部指到每条用例自己的临时目录；用例开始前逐个断言它们确实落在 pytest 的临时根之下。
2. `HUB_LAUNCHCTL` 换成记账的假件（只记参数，绝不转发）；`HUB_BUN` 不动，保持总闸的毒丸。
3. 旧系统那条链自己的五个落点（清单、配置根、频道根、代理运行目录、代理的 launchctl 开关）
   照 `tests/unit/test_provider_legacy_port.py` 的 `legacy_env` 夹具同样钉住。这五个变量名
   不在接口约定里，出处是那份既有夹具；本目录只拿它们做隔离与预置文件，断言不靠它们。
   它们指向**整场会话固定的一处**临时目录：旧链路的落点是导入时定死的，每条用例换一处的话，
   预置的文件就不是它真正会去写的那一份了。
4. 十五条旧接口在下线之前是活的。量「已下线」的用例一律先看路由清单
   （`le_util.require_retired`），清单里还有就当场判失败，不发请求。
5. 本目录不去列真实目录的条目（别的目录有一道那样的守卫），靠第 1 条的断言保证不落到真实位置。
6. 任何用例都不跟随跳转，不访问约定里那两个外部地址。
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
import os
import shutil
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import le_util as u

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 外面带进来就会改变预期的变量，一律清掉
_CLEARED = (
    u.LOCAL_ENV, u.PUBLIC_ENV,
    "HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD", "ACCESS_PASSWORD", "MOMENTS_WEB_PORT",
    "CLIPROXY_MGMT_KEY", "CLIPROXY_PORT", "CLIPROXY_API_KEY", "CLIPROXY_ANTHROPIC_COMPAT",
    "HUB_ALLOW_PRIVATE_BASE_URL", "HUB_BOTS_FILE",
    # 本机常备代理会把发往 127.0.0.1 的请求也劫走
    "http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "no_proxy", "NO_PROXY",
)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _seed_configs(configs):
    """在一个「旧系统配置根」里放好清单与全局配置。"""
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "providers.json").write_bytes(u.SEED_PROVIDERS_JSON)
    (configs / "_global.yml").write_bytes(u.SEED_GLOBAL_YML)
    return configs


@pytest.fixture(scope="session")
def legacy_side_base(tmp_path_factory):
    """旧系统那条链的落点，整场会话固定一处（见模块说明第 3 条）。只建目录，不设环境。"""
    return tmp_path_factory.mktemp("legacy-side")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, tmp_path_factory, monkeypatch, legacy_side_base, total_gate):
    """一切路径与接缝钉在临时目录；旧系统那边的文件每条用例开始前重新预置。"""
    temp_root = Path(os.path.realpath(str(tmp_path_factory.getbasetemp())))
    home = tmp_path / "fakehome"
    (home / ".claude" / "channels").mkdir(parents=True)
    dsh_home = tmp_path / "dsh-home"
    dsh_cfg = tmp_path / "configs-dsh"
    hub_legacy_cfg = tmp_path / "configs-legacy"
    channels = tmp_path / "channels"
    agents = tmp_path / "LaunchAgents"
    tgbot = tmp_path / "tgbot-home"
    for d in (dsh_home, dsh_cfg, hub_legacy_cfg, channels, agents, tgbot):
        d.mkdir()

    # 旧系统那条链：整场会话固定的那一处，内容每条用例重来
    side = legacy_side_base / "claudebotlife"
    assert os.path.realpath(str(side)).startswith(str(temp_root) + os.sep), side
    shutil.rmtree(str(side), ignore_errors=True)
    side_cfg = _seed_configs(side / "configs")
    side_channels = side / "channels"
    side_proxy = side / "proxy"
    side_channels.mkdir()
    side_proxy.mkdir()
    # 管理台自己的旧系统配置根里那份全局配置，以及假家目录下「默认位置」的那一套
    (hub_legacy_cfg / "_global.yml").write_bytes(u.SEED_GLOBAL_YML)
    home_side_cfg = _seed_configs(home / "claudebotlife" / "configs")

    for k in _CLEARED:
        monkeypatch.delenv(k, raising=False)
    paths = {
        "HOME": home,
        "USERPROFILE": home,
        "DSH_BOT_HOME": dsh_home,
        "CLAUDE_TGBOT_HOME": tgbot,
        "BOTLIFE_STATE_DB": tmp_path / "state.db",
        "CLAUDE_SETTINGS_PATH": home / ".claude" / "settings.json",
        "HUB_ENV_FILE": tmp_path / "hub.env",
        "HUB_CONFIGS_DIR": hub_legacy_cfg,
        "HUB_CONFIGS_DSH_DIR": dsh_cfg,
        "HUB_CHANNELS_DIR": channels,
        "HUB_LAUNCHAGENTS_DIR": agents,
        "HUB_RESTART_SCRIPT": tmp_path / "no-such-restart.sh",
        # 以下五个照 tests/unit/test_provider_legacy_port.py 的 legacy_env（见模块说明第 3 条）
        "CLAUDEBOT_CONFIG_DIR": side_cfg,
        "CLAUDEBOT_CHANNELS_ROOT": side_channels,
        "CLAUDEBOT_PROVIDER_PATH": side_cfg / "providers.json",
        "CLAUDEBOT_PROXY_DIR": side_proxy,
    }
    fake_exe, fake_log = u.make_recording_launchctl(tmp_path / "fake-launchctl")
    paths["HUB_LAUNCHCTL"] = fake_exe
    for k, v in paths.items():
        real = os.path.realpath(str(v))
        assert real.startswith(str(temp_root) + os.sep), "%s 没钉在临时目录里：%s" % (k, v)
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("CLAUDEBOT_PROXY_LAUNCHCTL", "off")
    monkeypatch.setenv("MOMENTS_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("HUB_RESTART_CMD", "/usr/bin/true")
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", "http://127.0.0.1:%d" % _free_port())
    assert os.environ.get("HUB_BUN") == total_gate.poison_bun, \
        "HUB_BUN 应当还是总闸的毒丸，实得 %r" % os.environ.get("HUB_BUN")

    return SimpleNamespace(
        tmp=tmp_path, home=home, dsh_home=dsh_home, dsh_cfg=dsh_cfg, hub_legacy_cfg=hub_legacy_cfg,
        env_file=tmp_path / "hub.env",
        seeded_providers=[side_cfg / "providers.json", home_side_cfg / "providers.json"],
        seeded_globals=[side_cfg / "_global.yml", hub_legacy_cfg / "_global.yml",
                        home_side_cfg / "_global.yml"],
        legacy_roots=[side, hub_legacy_cfg, home_side_cfg.parent],
        launchctl_calls=lambda: u.read_launchctl_calls(fake_log))


def _load_app():
    """重新导入管理台应用（配置在导入时被读，改了环境就要重载）。照 hub_ui 的做法。"""
    import db
    importlib.reload(db)
    assert Path(db.DB_PATH).resolve() != (REPO_ROOT / "state.db").resolve(), \
        "朋友圈库落到了仓库里的 state.db，BOTLIFE_STATE_DB 没生效"
    db.init()
    mod = sys.modules.get("moments.web")
    mod = importlib.reload(mod) if mod else importlib.import_module("moments.web")
    for attr in ("app", "application"):
        obj = getattr(mod, attr, None)
        if obj is not None and hasattr(obj, "test_client"):
            return obj
    factory = getattr(mod, "create_app", None)
    if callable(factory):
        return factory()
    raise AssertionError("moments.web 未导出可用的 Flask app")


@pytest.fixture
def make_app(sandbox, monkeypatch):
    """(**env) -> 管理台应用。给 None 的变量会被删掉。"""
    def _make(**env):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, str(v))
        app = _load_app()
        app.config["TESTING"] = True
        return app
    return _make


@pytest.fixture
def app(make_app):
    """默认应用：没设访问口令，两个入口配置项都没设。"""
    return make_app()


@pytest.fixture
def hub(app):
    return app.test_client()


@pytest.fixture
def make_client(make_app):
    def _make(**env):
        return make_app(**env).test_client()
    return _make


@pytest.fixture
def login():
    """走表单登录（application/x-www-form-urlencoded）。

    `host` 要与之后发请求用的 Host 相同：测试客户端按主机名存登录凭据，换一个主机名就不带了。
    """
    def _login(client, password=None, admin_password=None, host=u.LOCAL_HOST):
        form = {}
        if password is not None:
            form["password"] = password
        if admin_password is not None:
            form["admin_password"] = admin_password
        return client.post("/login", data=form, content_type="application/x-www-form-urlencoded",
                           environ_overrides={"HTTP_HOST": host})
    return _login


@pytest.fixture
def styles_env(tmp_path, monkeypatch):
    """把画风数据文件与示例图目录钉在临时目录。照搬 hub_ui 的同名夹具，理由见那边的说明：
    这几个路径是导入时求值的，不显式钉住就可能读到真实的画风数据。"""
    from moments import styles_routes as sr

    root = tmp_path / "novelai-skill"
    (root / "assets").mkdir(parents=True)
    monkeypatch.setattr(sr, "STYLES_PATH", root / "assets" / "styles.json")
    monkeypatch.setattr(sr, "DEFAULT_CONFIG_PATH", root / "assets" / "default_config.json")
    monkeypatch.setattr(sr, "SAMPLE_DIR", tmp_path / "samples")
    assert str(sr.STYLES_PATH).startswith(str(tmp_path)), "画风数据文件没钉在临时目录"
    return SimpleNamespace(root=root, path=sr.STYLES_PATH)
