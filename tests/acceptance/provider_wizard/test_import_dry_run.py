# -*- coding: utf-8 -*-
"""F7-2：import_history.py --dry-run 也检查"这个聊天在新系统里已经聊过"（INTERFACE 3.10.2）。"""
from _pw_util import FRIEND, OWNER, digest

SEEN = "这个聊天在新系统里已经聊过（账本里有会话段），正式导入会跳过：聊天记录和承诺都不会导入。"
UNREADABLE_HEAD = "读不了账本（"
UNREADABLE_TAIL = "），没法判断这个聊天在新系统里有没有聊过。"


def _lines(r):
    return [l for l in r.stdout.splitlines() if l.strip()]


def test_聊过_试算_打印会跳过的提示(make_ledger, old_channel, run_import):
    make_ledger("newbot", OWNER, "第一句")
    r = run_import("newbot", old_channel((OWNER,)), "--dry-run")
    assert SEEN in r.stdout


def test_聊过_试算_退出码为0(make_ledger, old_channel, run_import):
    make_ledger("newbot", OWNER, "第一句")
    r = run_import("newbot", old_channel((OWNER,)), "--dry-run")
    assert r.returncode == 0, r.stderr


def test_聊过_提示行排在原有两行之后(make_ledger, old_channel, run_import):
    make_ledger("newbot", OWNER, "第一句")
    lines = _lines(run_import("newbot", old_channel((OWNER,)), "--dry-run"))
    assert SEEN in lines
    assert lines.index(SEEN) >= 2
    assert "对方" in lines[0] and "条" in lines[0]
    assert "承诺" in lines[1]


def test_聊过_输出里没有聊天内容(make_ledger, old_channel, run_import):
    make_ledger("newbot", OWNER, "独一无二的悄悄话xyz")
    r = run_import("newbot", old_channel((OWNER,)), "--dry-run")
    assert "独一无二的悄悄话xyz" not in r.stdout + r.stderr


def test_聊过_试算不改账本(make_ledger, old_channel, run_import):
    p = make_ledger("newbot", OWNER, "第一句")
    before = (digest(p), digest(str(p) + "-wal"))
    run_import("newbot", old_channel((OWNER,)), "--dry-run")
    assert (digest(p), digest(str(p) + "-wal")) == before


def test_只有别人聊过_主人私聊没有会话段_不打印提示(make_ledger, old_channel, run_import):
    make_ledger("newbot", FRIEND, "别人的话")
    r = run_import("newbot", old_channel((OWNER,)), "--dry-run")
    assert r.returncode == 0
    assert SEEN not in r.stdout


def test_主人私聊id取旧频道access_json的allowFrom第一个(make_ledger, old_channel, run_import):
    make_ledger("newbot", FRIEND, "别人的话")
    r = run_import("newbot", old_channel((FRIEND, OWNER)), "--dry-run")
    assert SEEN in r.stdout


def test_账本存在但没有任何会话段_不打印提示(make_ledger, old_channel, run_import):
    make_ledger("newbot", None)
    r = run_import("newbot", old_channel((OWNER,)), "--dry-run")
    assert r.returncode == 0
    assert SEEN not in r.stdout


def test_账本不存在_只有原有两行_退出码0(sandbox, old_channel, run_import):
    r = run_import("freshbot", old_channel((OWNER,)), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert SEEN not in r.stdout
    assert UNREADABLE_HEAD not in r.stdout
    assert len(_lines(r)) == 2


def test_账本不存在_试算不创建state目录也不新建账本(sandbox, old_channel, run_import):
    run_import("freshbot", old_channel((OWNER,)), "--dry-run")
    assert not (sandbox["root"] / "bots" / "freshbot" / "state").exists()
    assert not (sandbox["root"] / "bots" / "freshbot" / "state" / "ledger.sqlite").exists()


def test_账本读不了_打印读不了与异常类名_退出码0(sandbox, old_channel, run_import):
    st = sandbox["root"] / "bots" / "brokenbot" / "state"
    st.mkdir(parents=True)
    (st / "ledger.sqlite").write_bytes(b"this is not a sqlite database at all" * 100)
    r = run_import("brokenbot", old_channel((OWNER,)), "--dry-run")
    assert r.returncode == 0, r.stderr
    line = next((l for l in r.stdout.splitlines() if l.startswith(UNREADABLE_HEAD)), "")
    assert line.endswith(UNREADABLE_TAIL), r.stdout
    cls = line[len(UNREADABLE_HEAD):-len(UNREADABLE_TAIL)]
    assert cls and cls.isidentifier(), f"括号里应是异常类名：{cls!r}"


def test_账本读不了_试算不改这个文件(sandbox, old_channel, run_import):
    st = sandbox["root"] / "bots" / "brokenbot" / "state"
    st.mkdir(parents=True)
    f = st / "ledger.sqlite"
    f.write_bytes(b"garbage" * 50)
    before = digest(f)
    run_import("brokenbot", old_channel((OWNER,)), "--dry-run")
    assert digest(f) == before
