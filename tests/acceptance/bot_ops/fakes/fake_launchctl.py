# -*- coding: utf-8 -*-
"""假 launchctl（测试侧数据脚本，不是测试用例）。

conftest 生成的 shim 以
    <shim> --cfg <cfg.json> --log <calls.jsonl> <launchctl 的参数...>
调用本脚本；`--cfg`/`--log` 前缀由本脚本剥掉。行为由 cfg.json 驱动：

  default       未见过的单元(label)的初始状态 {installed, running, pid}
  units         {label: {installed, running, pid}}，按 label 分别跟踪
  pid           每次 kickstart/bootstrap 后用的 pid（缺省沿用单元现值）
  delay         {子命令: 秒} —— 用来构造 LAUNCHCTL_TIMEOUT 场景
  kickstart_missing   true 时 kickstart 一律报「Could not find service」退出 3
  bootstrap_fail      true 时 bootstrap 退出 1
  print_text    直接指定 print 的原文（用来构造解析边界）

每次调用向 log 追一行 JSON：{"argv": [...], "cmd": ..., "label": ...}。
绝不碰真 launchctl、真 LaunchAgents。
"""
import json
import os
import sys
import time


def _parse_prefix(argv):
    cfg_path = log_path = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--cfg" and i + 1 < len(argv):
            cfg_path = argv[i + 1]
            i += 2
        elif a == "--log" and i + 1 < len(argv):
            log_path = argv[i + 1]
            i += 2
        else:
            rest.append(a)
            i += 1
    return cfg_path, log_path, rest


def _label_of(rest):
    """从 `gui/501/com.dsh-bot.x` 或裸 label 或 plist 路径里取 label。"""
    for a in rest:
        for part in str(a).replace("\\", "/").split("/"):
            if part.startswith("com."):
                return part.rsplit(".plist", 1)[0]
    return ""


def _load(path):
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return {}
    return {}


def _save(path, cfg):
    if path:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        os.replace(tmp, path)


def _log(path, rec):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _emit(out, err, code):
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(code)


def main():
    cfg_path, log_path, rest = _parse_prefix(sys.argv[1:])
    cfg = _load(cfg_path)
    cmd = rest[0] if rest else ""
    label = _label_of(rest)
    _log(log_path, {"argv": rest, "cmd": cmd, "label": label})

    delay = (cfg.get("delay") or {}).get(cmd)
    if delay:
        time.sleep(float(delay))

    units = cfg.setdefault("units", {})
    st = dict(units.get(label) if label in units else (cfg.get("default") or {}))
    pid = cfg.get("pid", st.get("pid"))

    if cmd == "print":
        text = cfg.get("print_text")
        if text is None:
            if st.get("installed") and st.get("running"):
                text = ("%s = {\n\tactive count = 1\n\tstate = running\n\tpid = %s\n"
                        "\tpath = /tmp/fake.plist\n}\n" % (label, st.get("pid")))
            elif st.get("installed"):
                text = '%s = {\n\tstate = not running\n\tpath = /tmp/fake.plist\n}\n' % label
            else:
                _emit("", 'Could not find service "%s" in domain for user gui: 501\n' % label, 3)
        _emit(text, "", 0)

    if cmd == "kickstart":
        if st.get("installed") and not cfg.get("kickstart_missing"):
            st["running"] = True
            if pid is not None:
                st["pid"] = pid
            units[label] = st
            _save(cfg_path, cfg)
            _emit("", "", 0)
        _emit("", 'Could not find service "%s" in domain for user gui: 501\n' % label, 3)

    if cmd == "bootstrap":
        if cfg.get("bootstrap_fail"):
            _emit("", "Bootstrap failed: 5: Input/output error\n", 1)
        st["installed"] = True
        st["running"] = True
        if pid is not None:
            st["pid"] = pid
        units[label] = st
        _save(cfg_path, cfg)
        _emit("", "", 0)

    if cmd == "bootout":
        if st.get("installed"):
            st["installed"] = False
            st["running"] = False
            st["pid"] = None
            units[label] = st
            _save(cfg_path, cfg)
            _emit("", "", 0)
        _emit("", "Boot-out failed: 3: No such process\n", 3)

    _emit("", "fake launchctl: unknown subcommand %r\n" % cmd, 1)


if __name__ == "__main__":
    main()
