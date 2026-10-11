# -*- coding: utf-8 -*-
"""假 bun（测试侧数据脚本，不是测试用例）。

conftest 生成的 shim 以 `<shim> --cfg <cfg.json> --log <calls.jsonl> <bun 的参数...>`
调用；前缀由本脚本剥掉。响应按 `responses` 列表顺序消费（每次调用弹掉一项），
弹空后用 `default`。每次调用向 log 追一行 JSON：argv、cwd、以及子进程 env 的
关键键（PATH/HOME/PYTHONPATH 等）——用来断言 register 子进程的 PATH 前置。

绝不真跑 bun / autostart.ts。
"""
import json
import os
import sys

KEYS = ("PATH", "HOME", "USERPROFILE", "DSH_BOT_HOME", "HUB_CONFIGS_DSH_DIR",
        "PYTHONPATH", "PWD")


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


def main():
    cfg_path, log_path, rest = _parse_prefix(sys.argv[1:])
    cfg = _load(cfg_path)
    queue = cfg.get("responses") or []
    resp = queue.pop(0) if queue else None
    if queue or resp is not None:
        cfg["responses"] = queue
        _save(cfg_path, cfg)
    if resp is None:
        resp = cfg.get("default") or {}
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": rest, "cwd": os.getcwd(),
                                "env": {k: os.environ.get(k) for k in KEYS}},
                               ensure_ascii=False) + "\n")
    out = resp.get("out") or ""
    err = resp.get("err") or ""
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(int(resp.get("exit", 0)))


if __name__ == "__main__":
    main()
