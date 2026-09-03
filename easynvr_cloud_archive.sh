#!/bin/bash
# =============================================================================
# easynvr-cloud-archive
# EasyNVR 录像本地碎分片 -> 按小时无损合并 -> 云盘归档 通用版
#
# 核心思路:
#   1. EasyNVR 默认把录像切成 ~64s 的碎分片存在本地, 长期不处理会撑爆磁盘
#   2. 本脚本把「足够老」的日期(D-n)按小时 ffmpeg concat 无损合并成
#      YYYYMMDD_HH.mp4, 以 900K 限速上传到 rclone 配置的云盘
#   3. 上传后做「本地/远端字节数一致」校验, 通过才删除本地该日期目录
#      (绝不先删后传, 杜绝丢录像)
#   4. 保留最近 PROTECT_N 天在本地, 保证 NVR 平台近端回放可用
#   5. 只处理一个通道、一个挂载点时可直接跑; 多通道请用 cron 循环/复制多份配置
#
# 调度策略(两层触发, 写在 cron 里每 5 分钟跑一次):
#   - 窗口制: 每天 WINDOW_START_MIN~WINDOW_END_MIN(默认凌晨 02:00-04:30) 执行归档
#   - 紧急制: 任意时刻磁盘使用率 >= URGENT_TRIGGER_PCT 立即执行, 降到 URGENT_STOP_PCT 停
#   - 窗口外 & 未超水位 -> 零动作退出(脚本自身开销极小)
#
# 运维开关(环境变量):
#   FORCE_RUN=1          窗口外手动强制执行一次(仍限速; 仍遵守 PROTECT_N)
#   ARCHIVE_YDAY=1       连「昨天」一起归档(把 PROTECT_N 临时降为 1; 慎用!)
#
# 用法:
#   EASYNVR_CFG=/path/to/config.conf bash easynvr_cloud_archive.sh
#   或: 把 config.conf 与脚本放同目录, 直接 bash easynvr_cloud_archive.sh
# =============================================================================

# ---------- 配置加载(优先级: 环境变量 EASYNVR_CFG > 同目录 config.conf > config.example.conf) ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "$EASYNVR_CFG" ]; then
  CFG="$EASYNVR_CFG"
elif [ -f "$SCRIPT_DIR/config.conf" ]; then
  CFG="$SCRIPT_DIR/config.conf"
else
  CFG="$SCRIPT_DIR/config.example.conf"
fi
[ -f "$CFG" ] || { echo "找不到配置文件: $CFG"; exit 1; }
# shellcheck disable=SC1090
. "$CFG"

# ---------- 必填校验 ----------
: "${CH:?config 缺少 CH(通道ID, 如 34020000001320000001)}"
: "${LOCAL_BASE:?config 缺少 LOCAL_BASE(本地录像根目录)}"
: "${DST_CH:?config 缺少 DST_CH(rclone remote + 云盘路径)}"
: "${MOUNT_POINT:?config 缺少 MOUNT_POINT(磁盘水位检测挂载点)}"

# ---------- 可调参数(默认值) ----------
MERGE_TMP_DIR="${MERGE_TMP_DIR:-/var/tmp/easynvr_merge_tmp}"
LOG_FILE="${LOG_FILE:-/var/log/easynvr_cloud_archive.log}"
LOCK_FILE="${LOCK_FILE:-/var/lock/easynvr_cloud_archive.lock}"
BANDWIDTH_LIMIT="${BANDWIDTH_LIMIT:-900K}"
WINDOW_START_MIN="${WINDOW_START_MIN:-120}"     # 02:00 = 2*60
WINDOW_END_MIN="${WINDOW_END_MIN:-270}"         # 04:30 = 4*60+30
URGENT_TRIGGER_PCT="${URGENT_TRIGGER_PCT:-90}"  # 磁盘 >=90% 立即归档
URGENT_STOP_PCT="${URGENT_STOP_PCT:-80}"        # 紧急模式下降到 80% 停
PROTECT_N="${PROTECT_N:-2}"                     # 本地保留最近 N 天供回放(默认保留今天+昨天)
FRAG_GLOB="${FRAG_GLOB:-*-64000.mp4}"           # NVR 碎分片文件名后缀(用于云盘残留清理)
FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"
RCLONE_BIN="${RCLONE_BIN:-rclone}"

log(){ echo "$(date '+%F %T') $*" >> "$LOG_FILE"; }

# ---------- 单实例锁(flock, 防 cron 重入) ----------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "另一同步进程在跑，跳过"
  exit 0
fi

# ---------- 当前磁盘水位 & 调度判定 ----------
USE=$(df "$MOUNT_POINT" | awk 'NR==2 {gsub("%","",$5); print $5}')
[ -z "$USE" ] && { log "无法获取 $MOUNT_POINT 磁盘水位, 退出"; exit 0; }

NOW_MIN=$(( $(date +%-H)*60 + $(date +%-M) ))
IN_WINDOW=0
[ "$NOW_MIN" -ge "$WINDOW_START_MIN" ] && [ "$NOW_MIN" -lt "$WINDOW_END_MIN" ] && IN_WINDOW=1
[ -n "$FORCE_RUN" ] && IN_WINDOW=1 && log "FORCE_RUN 手动强制执行"

if [ "$IN_WINDOW" -eq 0 ] && [ "$USE" -lt "$URGENT_TRIGGER_PCT" ]; then
  log "非窗口且磁盘 ${USE}%<${URGENT_TRIGGER_PCT}%, 零动作"
  exit 0
fi
MODE="window"; [ "$USE" -ge "$URGENT_TRIGGER_PCT" ] && MODE="urgent"
log "模式=${MODE} 磁盘=${USE}% 开始按小时合并归档 (PROTECT_N=${PROTECT_N})"

[ -d "$LOCAL_BASE" ] || { log "本地录像目录不存在: $LOCAL_BASE"; exit 0; }
mkdir -p "$MERGE_TMP_DIR"

# 本地只处理「早于保护期」的日期目录
# PROTECT_N=2 => 保留今天+昨天 => 归档 D-2 及更早
# ARCHIVE_YDAY=1 => 保留今天 => 昨天也归档(慎用, 会缩短近端回放窗口)
PROTECT_FROM=$(date -d "$((PROTECT_N - 1)) days ago" +%Y%m%d)
[ -n "$ARCHIVE_YDAY" ] && PROTECT_FROM=$(date +%Y%m%d)   # 临时只保护今天

# ---------- 工具函数 ----------
# 合并某日某小时: 把该小时全部碎分片 concat -c copy 无损合成单文件
# 注意: concat list 必须用绝对路径, 否则 ffmpeg 相对当前工作目录找不到源
merge_one_hour(){
  local d="$1" h="$2" src="$LOCAL_BASE/$d" out="$MERGE_TMP_DIR/${d}_${h}.mp4" list="$MERGE_TMP_DIR/.${d}_${h}.list"
  [ -f "$out" ] && return 0
  : > "$list"
  ls "$src"/*"${d}${h}"*.mp4 2>/dev/null | sort > "$list"
  if [ ! -s "$list" ]; then rm -f "$list"; return 0; fi
  sed -i "s|^|file '|;s|$|'|" "$list"
  "$FFMPEG_BIN" -v error -y -f concat -safe 0 -i "$list" -c copy -movflags +faststart "$out"
  local rc=$?
  rm -f "$list"
  if [ $rc -ne 0 ]; then rm -f "$out"; log "合并失败 ${d}_${h}"; return 1; fi
  return 0
}

# 归档某日: 该日全部小时 合并->上传->校验->清远端碎分片->删本地目录
archive_day(){
  local d="$1" src="$LOCAL_BASE/$d" ok=1 h hours out lsz rsz
  [ -d "$src" ] || return 0
  # 从碎分片名提取该日存在的小时列表 (YYYYMMDDHH...)
  hours=$(ls "$src"/*.mp4 2>/dev/null | sed "s|.*${d}\([0-9][0-9]\).*|\1|" | sort -u)
  for h in $hours; do
    merge_one_hour "$d" "$h" || { ok=0; break; }
    out="$MERGE_TMP_DIR/${d}_${h}.mp4"
    [ -f "$out" ] || continue
    "$RCLONE_BIN" copy "$out" "$DST_CH/$d/" --bwlimit "$BANDWIDTH_LIMIT" --log-file "$LOG_FILE" --log-level INFO 2>/dev/null
    # 上传后校验: 远端大小必须与本地一致, 否则视为失败并保留本地
    lsz=$(stat -c %s "$out")
    rsz=$("$RCLONE_BIN" lsl "$DST_CH/$d/${d}_${h}.mp4" 2>/dev/null | awk '{print $1}')
    if [ -z "$rsz" ] || [ "$lsz" != "$rsz" ]; then
      log "上传校验失败 ${d}_${h} (local=${lsz} remote=${rsz:-NA})"
      ok=0; break
    fi
    rm -f "$out"
  done
  if [ $ok -eq 1 ]; then
    # 云盘同日期下历史碎分片已全部并入 *_HH.mp4, 一并清掉避免重复占空间
    # (首次启用时若云盘已有大量历史碎文件, 建议先人工核对再交给脚本清理)
    "$RCLONE_BIN" delete "$DST_CH/$d/" --include "$FRAG_GLOB" 2>>"$LOG_FILE" || true
    rm -rf "$src"
    log "已归档并删除本地 ${d}, 并清理云盘 ${d} 碎分片"
  else
    log "归档 ${d} 未完成, 保留待下轮"
  fi
  return $ok
}

# ---------- 主流程: 遍历本地日期目录, 处理早于保护期的日期 ----------
for d in $(ls "$LOCAL_BASE" 2>/dev/null | grep -E '^[0-9]{8}$' | sort); do
  [ "$d" -ge "$PROTECT_FROM" ] && continue
  archive_day "$d"
  # 紧急模式: 磁盘已降到安全水位就提前收工
  if [ "$MODE" = "urgent" ]; then
    USE_NOW=$(df "$MOUNT_POINT" | awk 'NR==2 {gsub("%","",$5); print $5}')
    [ "$USE_NOW" -lt "$URGENT_STOP_PCT" ] && { log "紧急模式已降至 ${USE_NOW}%<${URGENT_STOP_PCT}%, 提前结束"; break; }
  fi
done

rm -rf "$MERGE_TMP_DIR"
log "本次结束 磁盘 $(df "$MOUNT_POINT" | awk 'NR==2 {gsub("%","",$5); print $5}')%"
exit 0
