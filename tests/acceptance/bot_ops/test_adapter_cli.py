# -*- coding: utf-8 -*-
"""适配服务验收：命令行启动时的三种拒绝启动。

依据：INTERFACE-适配服务.md 第 1、2 节。不读实现。
只测"拒绝启动"：每条都起一个子进程跑 `python -m moments.adapter_service`，
它应当打印一句原因后自己退出。不测能正常启动的情形，不去占 17990 端口。
"""
import pytest

from . import adapter_util as au
from .adapter_util import console_guard  # noqa: F401  夹具


def _token_file(tmp_path, content=au.ADAPTER_TOKEN):
    d = tmp_path / "console-token"
    d.mkdir(mode=0o700, exist_ok=True)
    f = d / "adapter.token"
    f.write_text(content, encoding="utf-8")
    f.chmod(0o600)
    return f


def _assert_refused(res, code):
    assert not res.timed_out, \
        "子进程没有自己退出（可能真的开始监听了），已被杀掉。它的输出：%s" % res.output[-300:]
    assert res.code == code, "退出码应为 %d，实际 %r。它的输出：%s" % (code, res.code, res.output[-300:])
    assert res.output, "拒绝启动时应打印一句原因，实际什么都没打印"
    assert not res.leftover, "子进程退出后还留着别的进程"


BAD_LISTEN = {
    "地址是0.0.0.0": {"DSH_CONSOLE_HOST": "0.0.0.0"},
    "地址是局域网地址": {"DSH_CONSOLE_HOST": "192.168.1.10"},
    "地址是IPv6任意地址": {"DSH_CONSOLE_HOST": "::"},
    "地址是外部主机名": {"DSH_CONSOLE_HOST": "example.com"},
    "端口是0": {"DSH_CONSOLE_PORT": "0"},
    "端口是65536": {"DSH_CONSOLE_PORT": "65536"},
    "端口是负数": {"DSH_CONSOLE_PORT": "-1"},
    "端口不是数字": {"DSH_CONSOLE_PORT": "abc"},
    "端口带小数": {"DSH_CONSOLE_PORT": "80.5"},
}


@pytest.mark.parametrize("bad", list(BAD_LISTEN.values()), ids=list(BAD_LISTEN))
def test_命令行_监听地址或端口不合规_退出码2_不监听(sandbox, tmp_path, console_guard, bad):
    env = {"DSH_CONSOLE_HOST": "127.0.0.1", "DSH_CONSOLE_PORT": str(au.free_port()),
           "DSH_CONSOLE_TOKEN_FILE": str(_token_file(tmp_path))}
    env.update(bad)
    res = au.run_cli(env)
    _assert_refused(res, 2)
    assert au.ADAPTER_TOKEN not in res.output, "拒绝启动的输出里出现了令牌"


def _missing(tmp_path):
    return tmp_path / "console-token" / "adapter.token"          # 根本没建


def _empty(tmp_path):
    return _token_file(tmp_path, "")


def _blank(tmp_path):
    return _token_file(tmp_path, " \n\t\n")


def _is_dir(tmp_path):
    d = tmp_path / "console-token" / "adapter.token"
    d.mkdir(parents=True)
    return d


BAD_TOKEN_FILE = {"文件不存在": _missing, "文件是空的": _empty, "文件里只有空白": _blank,
                  "路径是个目录读不了": _is_dir}


@pytest.mark.parametrize("make", list(BAD_TOKEN_FILE.values()), ids=list(BAD_TOKEN_FILE))
def test_命令行_令牌文件缺失或为空_退出码3_不监听(sandbox, tmp_path, console_guard, make):
    res = au.run_cli({"DSH_CONSOLE_HOST": "127.0.0.1", "DSH_CONSOLE_PORT": str(au.free_port()),
                      "DSH_CONSOLE_TOKEN_FILE": str(make(tmp_path))})
    _assert_refused(res, 3)
