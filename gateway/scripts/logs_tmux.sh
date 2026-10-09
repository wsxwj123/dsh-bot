#!/bin/bash
# 日志看板：给每个配好的 bot 起一个随时能 attach 的 tmux 会话（tg-<bot>-dsh），里面实时跟 gateway/scripts/logs.ts 的输出。
#   bash gateway/scripts/logs_tmux.sh
# 幂等：会话已存在就跳过，绝不关任何已有会话，更不碰旧系统的 tg-*-worker / tg-*-dispatcher。
# 开机自启：launchd 任务 com.dsh-bot.tmux-logs（bun gateway/scripts/autostart.ts install-logs 装），
# RunAtLoad + 每 5 分钟跑一次。launchd 拉起的进程 PATH 里不一定有 bun / tmux，所以两边都用绝对路径：
# install-logs 会把解析到的 BUN_BIN / TMUX_BIN 写进 plist，脚本里的默认值只是手工跑时的兜底。
# 输出：有终端时逐个 bot 打明细（手工跑就是这样）；launchd 拉起的（没有终端）只在有会话新建/失败时说话，
#   免得 launchd 的输出文件每 5 分钟涨几行。想强制打明细：LOGS_TMUX_VERBOSE=1。
# 环境变量（都有默认值，便于测试时覆盖）：
#   DSH_BOT_CONFIGS_DIR  bot 配置目录，默认 $DSH_BOT_HOME/configs 或 ~/.dsh-bot/configs
#   DSH_BOT_HOME         ~/.dsh-bot 的替代（和仓库其它地方同名）
#   BUN_BIN / TMUX_BIN   bun / tmux 可执行文件
set -u

REPO=$(cd "$(dirname "$0")/../.." && pwd) || { echo "logs_tmux：找不到仓库目录" >&2; exit 1; }
CONFIGS_DIR=${DSH_BOT_CONFIGS_DIR:-${DSH_BOT_HOME:-$HOME/.dsh-bot}/configs}

# bun / tmux 都用绝对路径：launchd 环境里 PATH 不可靠（和 autostart.ts 里 Bun.which('bun') ?? process.execPath 一个思路）
BUN=${BUN_BIN:-}
if [ -z "$BUN" ]; then
  if [ -x "$HOME/.bun/bin/bun" ]; then BUN="$HOME/.bun/bin/bun"; else BUN=$(command -v bun 2>/dev/null || true); fi
fi
if [ -z "$BUN" ]; then
  echo "logs_tmux：找不到 bun（装到 ~/.bun/bin/bun，或设 BUN_BIN）" >&2
  exit 1
fi

TMUX=${TMUX_BIN:-}
[ -z "$TMUX" ] && TMUX=$(command -v tmux 2>/dev/null || true)
if [ -z "$TMUX" ]; then
  echo "logs_tmux：找不到 tmux（设 TMUX_BIN，或 brew install tmux）" >&2
  exit 1
fi

if [ ! -d "$CONFIGS_DIR" ]; then
  echo "logs_tmux：没有配置目录 $CONFIGS_DIR（还没切一个 bot 过来？）"
  exit 0
fi

# 读配置文件顶层的 id:（yml 行首那一行），去掉行尾注释、引号和空白；读不到返回 1
read_bot_id() {
  local line v
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      'id:'*)
        v=${line#id:}
        v=${v%%#*}
        v=${v#"${v%%[![:space:]]*}"}
        v=${v%"${v##*[![:space:]]}"}
        case "$v" in
          '"'*'"'|"'"*"'") v=${v#?}; v=${v%?} ;;
        esac
        printf '%s' "$v"
        return 0
        ;;
    esac
  done < "$1"
  return 1
}

# launchd 里 stdout 是文件（不是终端）：安静模式，只有真发生事情才写日志
verbose=1
[ -t 1 ] || verbose=0
[ "${LOGS_TMUX_VERBOSE:-}" = "1" ] && verbose=1

found=0 created=0 skipped=0 failed=0
for f in "$CONFIGS_DIR"/*.yml; do
  [ -f "$f" ] || continue
  id=$(read_bot_id "$f") || { [ "$verbose" = 1 ] && echo "logs_tmux：$(basename "$f") 里没有 id，跳过"; continue; }
  case "$id" in
    ''|*[!A-Za-z0-9_-]*) [ "$verbose" = 1 ] && echo "logs_tmux：$(basename "$f") 的 id「$id」不能用（只能字母、数字、下划线、连字符），跳过"; continue ;;
  esac
  found=$((found + 1))
  name="tg-$id-dsh"
  # -t 加 = 前缀按名字精确匹配：tmux 默认前缀匹配，不加的话 tg-bot3 的查询会命中 tg-bot3-dispatcher
  if "$TMUX" has-session -t "=$name" 2>/dev/null; then
    [ "$verbose" = 1 ] && echo "logs_tmux：$name 已存在，跳过"
    skipped=$((skipped + 1))
    continue
  fi
  cmd="cd \"$REPO\" && \"$BUN\" gateway/scripts/logs.ts --config \"$f\" -f --chat"
  if "$TMUX" new -d -s "$name" "$cmd"; then
    echo "logs_tmux：$name 新建（跟 $(basename "$f") 的实时日志）"
    created=$((created + 1))
  else
    echo "logs_tmux：$name 没建起来（tmux 出错）" >&2
    failed=$((failed + 1))
  fi
done

if [ "$found" -eq 0 ]; then
  echo "logs_tmux：$CONFIGS_DIR 里没找到 bot 配置。"
else
  summary="logs_tmux：共 $found 个 bot，建了 $created 个、跳过 $skipped 个"
  [ "$failed" -gt 0 ] && summary="$summary、失败 $failed 个"
  if [ "$verbose" = 1 ] || [ "$created" -gt 0 ] || [ "$failed" -gt 0 ]; then
    echo "$summary。"
  fi
fi
[ "$failed" -gt 0 ] && exit 1
exit 0
