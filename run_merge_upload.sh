#!/bin/bash
# merge_upload 全自动入口：flock 防并发 + token 过期自动刷新重试
# 用法: run_merge_upload.sh [--all|日期...]   默认处理昨天+今天
LOCK=/var/lock/merge_upload.lock
LOG=/var/log/merge_upload.log
B=/opt/easynvr-cloud-archive

exec 9>"$LOCK"
flock -n 9 || { echo "$(date '+%F %T') skip: another instance running" >> "$LOG"; exit 0; }

if [ "${1:-}" = "--all" ]; then
  ARGS=()
elif [ $# -gt 0 ]; then
  ARGS=("$@")
else
  ARGS=("$(date -d '-1 day' +%Y%m%d)" "$(date +%Y%m%d)")
fi

for attempt in 1 2; do
  /usr/bin/python3 "$B/merge_upload.py" "${ARGS[@]}" >> "$LOG" 2>&1
  rc=$?
  if [ $rc -eq 42 ]; then
    echo "$(date '+%F %T') token expired, try refresh (attempt $attempt)" >> "$LOG"
    /usr/bin/python3 "$B/token_refresh.py" >> "$LOG" 2>&1
    trc=$?
    if [ $trc -ne 0 ]; then
      echo "$(date '+%F %T') token refresh unavailable rc=$trc" >> "$LOG"
      exit 42
    fi
  else
    exit $rc
  fi
done
exit 0
