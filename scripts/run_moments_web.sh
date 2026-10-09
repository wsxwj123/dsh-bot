#!/bin/bash
# launchd wrapper: 启动 moments web 服务
# 用 bash wrapper 包一层，避免 launchd 直接 exec pyenv python 时 dyld 卡住
set -e
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export MOMENTS_WEB_PORT="${MOMENTS_WEB_PORT:-8765}"
# 仓库位置按本脚本自身定位。写死 ~/claudebotlife 的话，仓库克隆在别处 = 服务起不来，
# 而 plist 的 WorkingDirectory 已经指对了也白搭（这里的 cd 会把它顶掉）。
cd "$(cd "$(dirname "$0")/.." && pwd)"
# 解释器必须钉死：上面那行 PATH 把 Homebrew 的 python3（3.14，没装 flask）排在第一位，
# 直接 `exec python3 -m moments.web` 会 ModuleNotFoundError 起不来。照旧仓
# scripts/run_moments_web.sh 的口径钉到装了 flask 的 pyenv 3.10.11。
PY="$HOME/.pyenv/versions/3.10.11/bin/python3"
if [ ! -x "$PY" ]; then
  # 退化成"PATH 前置 pyenv shims"，让 pyenv 自己挑默认版本：这台机器换过/没装过
  # 3.10.11 时，钉死一个不存在的路径等于把服务钉死；挑不到时 exec 照常报
  # command not found，不静默换一个没装依赖的解释器。
  export PATH="$HOME/.pyenv/shims:$PATH"
  PY=python3
fi
exec "$PY" -u -m moments.web
