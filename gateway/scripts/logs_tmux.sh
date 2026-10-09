#!/bin/bash
# 日志看板：给每个启用中的 bot 起一个随时能 attach 的 tmux 会话（tg-<bot>-dsh），里面实时跟 gateway/scripts/logs.ts 的输出。
#   bash gateway/scripts/logs_tmux.sh
# 幂等：会话已存在就跳过，绝不关任何已有会话，更不碰旧系统的 tg-*-worker / tg-*-dispatcher。
# 名字与安全：bot 名从配置文件名取（bot2.yml → bot2），只认 ^[A-Za-z0-9._-]+$，不合格的文件名跳过并写明原因；
#   拼给 tmux 的命令串里路径一律走 sq 用单引号引用（内部的单引号按 '\'' 转义），不靠双引号插值。
# 启用判定：配置顶层写了 enabled 假值（false/no/off/0）的 bot 不建会话——切回旧栈的 bot 停在那边，
#   不该再看板（口径与 dsh-bot 侧 config_loader.list_enabled_bots 一致：没写字段 = 启用）。
# 多字节的坑：中文标点紧跟 $var 时，bash 3.2 在 UTF-8 locale 下会把标点的首字节吃进变量名
#   （launchd 里报 "summary…: unbound variable"，手工跑（C locale）不复现）；这里一律写 ${var} 断开。
# 开机自启：launchd 任务 com.dsh-bot.tmux-logs（bun gateway/scripts/autostart.ts install-logs 装），
#   RunAtLoad + 每 5 分钟跑一次。launchd 拉起的进程 PATH 里不一定有 bun / tmux，所以两边都用绝对路径：
#   install-logs 会把解析到的 BUN_BIN / TMUX_BIN 写进 plist，脚本里的默认值只是手工跑时的兜底。
# 输出：有终端时逐个 bot 打明细（手工跑就是这样）；launchd 拉起的（没有终端）只在有新建/失败、
#   文件名或配置不合格时说话，免得 launchd 的输出文件每 5 分钟涨几行。想强制打明细：LOGS_TMUX_VERBOSE=1。
# 环境变量（都有默认值，便于测试时覆盖）：
#   DSH_BOT_CONFIGS_DIR  bot 配置目录，默认 $DSH_BOT_HOME/configs 或 ~/.dsh-bot/configs
#   DSH_BOT_HOME         ~/.dsh-bot 的替代（和仓库其它地方同名）
#   BUN_BIN / TMUX_BIN   bun / tmux 可执行文件
set -u

REPO=$(cd "$(dirname "$0")/../.." && pwd) || { echo "logs_tmux：找不到仓库目录" >&2; exit 1; }
CONFIGS_DIR=${DSH_BOT_CONFIGS_DIR:-${DSH_BOT_HOME:-$HOME/.dsh-bot}/configs}

# 拼进 tmux 命令串的路径都过这里：整体用单引号包住，内部的单引号换成 '\''（POSIX shell 安全）
# 逐字符拼接，不用 ${s//\'/...}：bash 3.2（macOS 自带的 /bin/bash）对替换串里的反斜杠处理不可靠
sq() {
  local s=$1 out="'" i c
  for ((i = 0; i < ${#s}; i++)); do
    c=${s:i:1}
    case "$c" in
      "'") out="$out'\''" ;;
      *) out="$out$c" ;;
    esac
  done
  printf "%s'" "$out"
}

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
  echo "logs_tmux：没有配置目录 ${CONFIGS_DIR}（还没切一个 bot 过来？）"
  exit 0
fi

# 文件顶层的 id:（yml 行首那一行），去掉行尾注释、引号和空白；读不到返回 1。
# 它不拼进命令串，只用来确认"这是个 bot 配置"（没有 id 的 yml、id 坏了的 yml，logs.ts 也加载不了）
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

# 这个 bot 启用没有：行首（顶层）的 enabled 取值判定，与 dsh-bot 侧的 list_enabled_bots 一个口径。
# 没写 = 启用；空值按没写算；只有显式假值（false/no/off/0，不分大小写）才停；
# 带引号的 '"false"' 是字符串（非空即启用）。缩进的 enabled（别的段里的键）不算。
bot_enabled() {
  local line v
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      'enabled:'*) ;;
      *) continue ;;
    esac
    v=${line#enabled:}
    v=${v%%#*}
    v=${v#"${v%%[![:space:]]*}"}
    v=${v%"${v##*[![:space:]]}"}
    case "$v" in
      '""'|"''") return 1 ;;        # 空字符串是假值
      '"'*|"'"*) return 0 ;;        # 带引号的非空串是字符串，算启用
      false|False|FALSE|no|No|NO|off|Off|OFF|0) return 1 ;;
    esac
    return 0
  done < "$1"
  return 0
}

# launchd 里 stdout 是文件（不是终端）：安静模式，只有真发生事情才写日志
verbose=1
[ -t 1 ] || verbose=0
[ "${LOGS_TMUX_VERBOSE:-}" = "1" ] && verbose=1

found=0 created=0 skipped=0 failed=0
for f in "$CONFIGS_DIR"/*.yml; do
  [ -f "$f" ] || continue
  base=${f##*/}
  stem=${base%.yml}
  # bot 名从文件名取，只留白名单内的：带引号、$、; 等的名字不往 tmux 的命令串里拼，跳过并明确说一句
  case "$stem" in
    ''|*[!A-Za-z0-9._-]*)
      echo "logs_tmux：$base 的文件名不能用（只允许字母、数字、点、下划线、连字符），跳过"
      continue
      ;;
  esac
  id=$(read_bot_id "$f") || { [ "$verbose" = 1 ] && echo "logs_tmux：$base 里没有 id，跳过"; continue; }
  case "$id" in
    ''|*[!A-Za-z0-9_-]*) [ "$verbose" = 1 ] && echo "logs_tmux：$base 的 id「${id}」不能用（只能字母、数字、下划线、连字符），跳过"; continue ;;
  esac
  if ! bot_enabled "$f"; then
    [ "$verbose" = 1 ] && echo "logs_tmux：$base 标记为停用（enabled 写了假值），跳过"
    continue
  fi
  found=$((found + 1))
  name="tg-$stem-dsh"
  # -t 加 = 前缀按名字精确匹配：tmux 默认前缀匹配，不加的话 tg-bot3 的查询会命中 tg-bot3-dispatcher
  if "$TMUX" has-session -t "=$name" 2>/dev/null; then
    [ "$verbose" = 1 ] && echo "logs_tmux：$name 已存在，跳过"
    skipped=$((skipped + 1))
    continue
  fi
  # --config 用校验过的文件名重建（不是原始 glob 结果）；整个命令串的路径经 sq 引用后交给 tmux
  cmd="cd $(sq "$REPO") && $(sq "$BUN") gateway/scripts/logs.ts --config $(sq "$CONFIGS_DIR/$base") -f --chat"
  if "$TMUX" new -d -s "$name" "$cmd"; then
    echo "logs_tmux：$name 新建（跟 $base 的实时日志）"
    created=$((created + 1))
  else
    echo "logs_tmux：$name 没建起来（tmux 出错）" >&2
    failed=$((failed + 1))
  fi
done

if [ "$found" -eq 0 ]; then
  echo "logs_tmux：$CONFIGS_DIR 里没有启用中的 bot 配置。"
else
  summary="logs_tmux：共 $found 个 bot，建了 $created 个、跳过 $skipped 个"
  [ "$failed" -gt 0 ] && summary="${summary}、失败 $failed 个"
  if [ "$verbose" = 1 ] || [ "$created" -gt 0 ] || [ "$failed" -gt 0 ]; then
    echo "${summary}。"
  fi
fi
[ "$failed" -gt 0 ] && exit 1
exit 0
