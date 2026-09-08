#!/bin/bash
# epaper — ZKC42V 4.2" 三色电子价签 macOS 驱动
# 用法:
#   ./epaper.sh img <png图片>           把图片显示到价签
#   ./epaper.sh clock                   显示当前时间(黑底白字时钟)
#   ./epaper.sh quotas                  拉取 codex/grok/kimi/opencode-go/Ollama Pro/Windsurf 额度并显示
#   ./epaper.sh quotas --no-send        只生成图/frame，不 BLE 推送
#   ./epaper.sh quotas-loop [seconds] [--start HH:MM --end HH:MM]  周期刷新（默认 900s，可限定活跃时段）
#   ./epaper.sh time                    同步价签时钟
#   ./epaper.sh scan                    扫描附近 BLE 设备
#   ./epaper.sh inspect <UUID>          查看设备 GATT Profile
set -e
cd "$(dirname "$0")"

# Prefer a Python that has Pillow (Homebrew 3.11 often; bare `python3` may be Xcode).
if [ -z "${PYTHON:-}" ]; then
  for c in python3.11 python3.12 python3 /opt/homebrew/bin/python3.11 /opt/homebrew/opt/python@3.11/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import PIL" 2>/dev/null; then
      PYTHON=$(command -v "$c")
      break
    fi
    if [ -x "$c" ] && "$c" -c "import PIL" 2>/dev/null; then
      PYTHON=$c
      break
    fi
  done
  PYTHON="${PYTHON:-python3}"
fi

# 近的那台价签（CoreBluetooth UUID）。用 scan 找到后把它填到这里或设成环境变量
#  $EPAPER_UUID。/!\ 这是你的私有价签标识，别提交你自己的 UUID。
export EPAPER_UUID="${EPAPER_UUID:-YOUR_TAG_UUID-0000-0000-0000-000000000000}"
export EPAPER_QUOTA_OUT="${EPAPER_QUOTA_OUT:-/tmp}"
# Default refresh interval for quotas-loop / launchd (seconds)
export EPAPER_QUOTA_INTERVAL="${EPAPER_QUOTA_INTERVAL:-900}"

usage() { sed -n '3,11p' "$0"; exit 0; }

run_quotas() {
  # args after "quotas": optional --no-send / --json / --loop N
  local send=1
  local extra=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-send) send=0; shift ;;
      --json) extra+=(--json); shift ;;
      --loop) extra+=(--loop "$2"); shift 2 ;;
      --start|--end) extra+=("$1" "$2"); shift 2 ;;
      --out-dir) export EPAPER_QUOTA_OUT="$2"; shift 2 ;;
      -h|--help) usage ;;
      *) extra+=("$1"); shift ;;
    esac
  done
  local cmd=("$PYTHON" -m quotas --out-dir "$EPAPER_QUOTA_OUT")
  cmd+=("${extra[@]}")
  if [ "$send" -eq 1 ]; then
    cmd+=(--send --bleprobe ./build/bleprobe)
  fi
  PYTHONPATH="./tools${PYTHONPATH:+:$PYTHONPATH}" "${cmd[@]}"
}

case "${1:-}" in
  img)
    [ -n "${2:-}" ] || usage
    "$PYTHON" tools/make_image.py "$2" --out /tmp/epaper-frame.bin
    ./build/bleprobe send "$EPAPER_UUID" /tmp/epaper-frame.bin
    ;;
  clock)
    "$PYTHON" tools/clock_image.py
    "$PYTHON" tools/make_image.py /tmp/epaper-clock.png --out /tmp/epaper-frame.bin
    ./build/bleprobe send "$EPAPER_UUID" /tmp/epaper-frame.bin
    ;;
  quotas)
    shift
    run_quotas "$@"
    ;;
  quotas-loop)
    # periodic mode: default 900s (15 min); optional active window 09:00-18:00
    interval="${2:-$EPAPER_QUOTA_INTERVAL}"
    shift 2 2>/dev/null || :
    run_quotas --loop "$interval" "$@"
    ;;
  time)
    # SET_TIME(0x20): 4-byte BE unix + tz(hours) + mode
    # mode: 0=PICTURE(hold custom image; still force-GUI once → may flash)
    #       1=CALENDAR  2=CLOCK (minute redraw; will overwrite any custom image)
    # WARNING: SET_TIME always triggers a firmware GUI refresh. Use mode 0, then
    # re-push your image (quotas/img) if the panel was showing a custom frame.
    MODE="${2:-0}"
    CMD=$("$PYTHON" -c "
import time
ts=int(time.time()); tz=8; mode=int('${MODE}')
print(f'20{ts:08x}{tz:02x}{mode:02x}')
")
    echo "SET_TIME cmd=$CMD (mode=$MODE; 0=picture 1=calendar 2=clock)"
    ./build/bleprobe seq "$EPAPER_UUID" 5 "62750002-D828-918D-FB46-B6C11C675AEC:$CMD"
    ;;
  scan) ./build/bleprobe scan "${2:-12}" ;;
  inspect) [ -n "${2:-}" ] || usage; ./build/bleprobe inspect "$2" ;;
  *) usage ;;
esac
