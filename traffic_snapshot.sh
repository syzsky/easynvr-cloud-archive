#!/bin/bash
# =============================================================================
# traffic_snapshot.sh: 每小时记录网卡累计字节, 供日报计算当日流量
# 用法(cron, 每小时 1 分):
#   1 * * * * /opt/easynvr-cloud-archive/traffic_snapshot.sh eth0 /root/.traffic_history
#
# 2026-10 修复: 快照文件长期为空(日报「今日流量」一直取不到数)
#   - 网卡名写错时脚本仍写 0 0 或静默失败 → 现在先校验 /sys/class/net/<iface> 存在
#   - 目录不存在导致重定向失败 → 现在 mkdir -p 后再写入
#   - 读不到计数不再写入脏数据(退出码 1), 便于 cron 日志里发现
# =============================================================================
IFACE="${1:-eth0}"
SNAP_FILE="${2:-/root/.traffic_history}"

if [ ! -d "/sys/class/net/${IFACE}" ]; then
  echo "$(date '+%F %T') traffic_snapshot: 网卡 ${IFACE} 不存在, 可用: $(ls /sys/class/net | tr '\n' ' ')" >&2
  exit 1
fi

TX=$(cat "/sys/class/net/${IFACE}/statistics/tx_bytes" 2>/dev/null)
RX=$(cat "/sys/class/net/${IFACE}/statistics/rx_bytes" 2>/dev/null)
if ! [[ "$TX" =~ ^[0-9]+$ && "$RX" =~ ^[0-9]+$ ]]; then
  echo "$(date '+%F %T') traffic_snapshot: 读取 ${IFACE} 计数失败(tx=$TX rx=$RX)" >&2
  exit 1
fi

mkdir -p "$(dirname "$SNAP_FILE")" 2>/dev/null
echo "$(date +%s) ${TX} ${RX}" >> "$SNAP_FILE" || { echo "$(date '+%F %T') traffic_snapshot: 写入 ${SNAP_FILE} 失败" >&2; exit 1; }

# 只保留最近 40 天, 防止无限增长
tail -n 960 "$SNAP_FILE" > "${SNAP_FILE}.tmp" 2>/dev/null && mv "${SNAP_FILE}.tmp" "$SNAP_FILE"
