#!/bin/bash
# link_monitor.sh: 归档链路健康监控(本机Alist WebDAV + 云盘连通), 断链实时告警
SEND_KEY="YOUR_SERVERCHAN_SENDKEY"
STATE_FILE="/tmp/.link_monitor_state"

curl -s -o /dev/null --connect-timeout 10 --max-time 15 http://127.0.0.1:8080/dav/
WEBDAV_OK=$?
rclone lsd aliyun: >/dev/null 2>&1
CLOUD_OK=$?

FAIL=0
[ $WEBDAV_OK -ne 0 ] && FAIL=$((FAIL+1))
[ $CLOUD_OK -ne 0 ] && FAIL=$((FAIL+1))

COUNT=0; [ -f "$STATE_FILE" ] && COUNT=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
if [ "$FAIL" -gt 0 ]; then
  COUNT=$((COUNT+1)); echo "$COUNT" > "$STATE_FILE"
  if [ "$COUNT" -eq 3 ]; then
    W="失败"; [ $WEBDAV_OK -eq 0 ] && W="OK"
    C="失败"; [ $CLOUD_OK -eq 0 ] && C="OK"
    MSG="归档链路异常(连续3次检测失败)\n- 本机Alist WebDAV(127.0.0.1:8080): ${W}\n- 云盘(aliyun): ${C}\n请检查本机 alist/watchdog 与隧道状态"
    curl -s -X POST "https://sctapi.ftqq.com/${SEND_KEY}.send" --data-urlencode "title=归档链路异常告警" --data-urlencode "desp=${MSG}" >/dev/null
  fi
else
  if [ "$COUNT" -ge 3 ]; then
    MSG="归档链路已恢复\n- 本机Alist WebDAV: OK\n- 云盘(aliyun): OK"
    curl -s -X POST "https://sctapi.ftqq.com/${SEND_KEY}.send" --data-urlencode "title=归档链路已恢复" --data-urlencode "desp=${MSG}" >/dev/null
  fi
  echo 0 > "$STATE_FILE"
fi

