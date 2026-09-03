#!/bin/bash
# =============================================================================
# monitor.sh 示例: 服务器存活监控(宕机告警)
#
# 重要前提: 被监控机(如 NVR 服务器)宕机后无法自报, 本脚本必须放在
#           「独立于被监控机的另一台常开主机」上运行!
#
# 用法: 把 TARGET/PORT 改成被监控机, SEND_KEY 换成自己的 Server酱 key,
#       配置 cron 每 5 分钟跑一次:
#   */5 * * * * /opt/easynvr-cloud-archive/monitor.sh
#
# 行为: 连续 THRESH 次(约 15 分钟)探测失败 -> 推送「疑似宕机」;
#       恢复后推送「已恢复」。状态存于 /root/.target_alive_state
# =============================================================================
SEND_KEY="YOUR_SERVERCHAN_SENDKEY"
TARGET="192.0.2.10"          # 被监控机 IP
PORT=22                      # 探测端口(建议用 SSH 端口即可判断存活)
THRESH=3                     # 连续失败几次后告警
STATE_FILE="/root/.target_alive_state"

[ "$SEND_KEY" = "YOUR_SERVERCHAN_SENDKEY" ] && { echo "请先配置 SEND_KEY"; exit 1; }

# TCP 连通性探测(不依赖 nc, 用 bash /dev/tcp)
if timeout 5 bash -c "cat < /dev/null > /dev/tcp/${TARGET}/${PORT}" 2>/dev/null; then
  CUR=alive
else
  CUR=dead
fi

PREV=$(cat "$STATE_FILE" 2>/dev/null || echo alive)
if [ "$CUR" = "dead" ]; then
  N=$(( $(cat "${STATE_FILE}.fail" 2>/dev/null || echo 0) + 1 ))
  echo "$N" > "${STATE_FILE}.fail"
  if [ "$N" -ge "$THRESH" ] && [ "$PREV" != "dead" ]; then
    curl -s -X POST "https://sctapi.ftqq.com/${SEND_KEY}.send" \
      --data-urlencode "title=⚠️ ${TARGET} 疑似宕机" \
      --data-urlencode "desp=${TARGET}:${PORT} 连续 ${N} 次探测失败(约 $((N*5)) 分钟无响应), 请检查。" >/dev/null
    echo dead > "$STATE_FILE"
  fi
else
  rm -f "${STATE_FILE}.fail"
  if [ "$PREV" = "dead" ]; then
    curl -s -X POST "https://sctapi.ftqq.com/${SEND_KEY}.send" \
      --data-urlencode "title=✅ ${TARGET} 已恢复" \
      --data-urlencode "desp=${TARGET}:${PORT} 探测恢复正常。" >/dev/null
  fi
  echo alive > "$STATE_FILE"
fi
