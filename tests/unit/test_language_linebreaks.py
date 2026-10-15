# -*- coding: utf-8 -*-
"""补充文件 11.3：语言名里的「换行」与 Python ``str.splitlines`` 同一个口径。

原先只拒回车与换行，可 ``str.splitlines`` 还会在下面 8 个字符处断行。语言名原样写进提示词，
这些字符一样会弄乱提示词的行，所以出现在值里任何位置都要 422：要在去首尾空白之前就拒，
不然放在两头的会被 ``strip`` 悄悄吃掉，放在中间的原样写进去。
"""
import pytest

from moments import adapter_service as svc

#: ``str.splitlines`` 认、而回车与换行之外的 8 个换行
EXTRA_BREAKS = ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


@pytest.mark.parametrize("ch", EXTRA_BREAKS, ids=lambda c: "U+%04X" % ord(c))
def test_语言名带splitlines认的另外8种换行_放中间或两头都拒收422(ch):
    assert ("英" + ch + "语").splitlines() == ["英", "语"], "这个字符不是 str.splitlines 认的换行"
    assert svc._language(" 英语 ", "text_language") == "英语"      # 对照：正常的语言名照收
    for value in ("英%s语" % ch, "英语%s" % ch, "%s英语" % ch):
        with pytest.raises(svc.Refuse) as e:
            svc._language(value, "text_language")
        assert (e.value.status, e.value.code) == (422, "invalid"), repr(value)
