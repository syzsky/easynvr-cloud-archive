#!/bin/bash
# =============================================================================
# run_merge_upload.sh: 合并+上传全自动入口(flock 防并发 + token 自愈重试)
#
# 用法:
#   run_merge_upload.sh                 # 常规调度: 窗口内处理已封盘日期, 窗口外零动作退出
#   run_merge_upload.sh 20261019        # 补传指定日期(不受上传窗口限制)
#   run_merge_upload.sh --force         # 强制在窗口外跑一次(仍遵守封盘/静置规则)
#   run_merge_upload.sh --all           # 全量重扫所有本地日期目录(谨慎, 会绕过保留窗口)
#
# cron(推荐: 只在 02:00-03:59 每 10 分钟跑):
#   */10 2-3 * * * /opt/easynvr-cloud-archive/run_merge_upload.sh
#   —— 原 */10 * * * * 全天跑导致: 白天占上行带宽 + 整点即合并当前小时(末片被截断约 2MB)
# =============================================================================
LOCK=/var/lock/merge_upload.lock
LOG=/var/log/merge_upload.log
B=/opt/easynvr-cloud-archive

exec 9>"$LOCK"
flock -n 9 || { echo "$(date '+%F %T') skip: another instance running" >> "$LOG"; exit 0; }

mode="window"
case "${1:-}" in
  --force) mode="force"; shift; export FORCE=1 ;;
  --all)   mode="all";   shift ;;
esac

ARGS=("$@")
if [ "$mode" = "all" ]; then
  # 显式枚举所有日期目录 → 绕过窗口与保留窗口, 用于故障恢复后全量重扫
  LOCAL_ROOT="${LOCAL_ROOT:-/data/easynvr/r/easynvr_rec}"
  mapfile -t ARGS < <(find "$LOCAL_ROOT" -maxdepth 3 -type d -regextype posix-extended \
                          -regex '.*/[0-9]{8}$' 2>/dev/null | xargs -r -n1 basename | sort -u)
fi

for attempt in 1 2; do
  /usr/bin/python3 "$B/merge_upload.py" "${ARGS[@]}" >> "$LOG" 2>&1
  rc=$?
  if [ $rc -eq 42 ]; then
    echo "$(date '+%F %T') token expired(401), 触发自愈刷新 (attempt $attempt)" >> "$LOG"
    /usr/bin/python3 "$B/token_refresh.py" >> "$LOG" 2>&1
    trc=$?
    if [ $trc -ne 0 ]; then
      echo "$(date '+%F %T') token 自愈失败 rc=$trc, 放弃本轮(已记入 .token_state.json)" >> "$LOG"
      exit 42
    fi
    continue
  fi
  exit $rc
done
exit 0
