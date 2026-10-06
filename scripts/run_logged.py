#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开机自启的 Python 周边用：把脚本的输出写进按大小轮转的日志（5MB × 3 份），不会无限长。

用法：python3 scripts/run_logged.py <日志文件> <脚本> [脚本参数…]
"""
import os
import runpy
import sys


class RotatingLog:
    def __init__(self, path: str, max_bytes: int = 5 * 1024 * 1024, keep: int = 3):
        self.path, self.max, self.keep = path, max_bytes, keep
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)

    def write(self, s: str) -> int:
        if not s:
            return 0
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) + len(s.encode("utf-8")) > self.max:
                for i in range(self.keep - 1, 0, -1):
                    if os.path.exists(f"{self.path}.{i}"):
                        os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
                os.replace(self.path, f"{self.path}.1")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(s)
        except OSError:
            pass
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def main() -> None:
    if len(sys.argv) < 3:
        print("用法：run_logged.py <日志文件> <脚本> [参数…]", file=sys.stderr)
        sys.exit(2)
    log, script = sys.argv[1], os.path.abspath(sys.argv[2])
    sys.stdout = sys.stderr = RotatingLog(log)
    sys.argv = [script, *sys.argv[3:]]
    sys.path.insert(0, os.path.dirname(script))
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
