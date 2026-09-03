#!/bin/bash
# traffic_snapshot.sh: 每小时记录一次网卡累计字节, 供日报计算当日流量
# 用法: 写进被监控机 cron(每小时 1 分):
#   1 * * * * /opt/easynvr-cloud-archive/traffic_snapshot.sh eth0 /root/.traffic_history
# 说明: eth0 换成实际网卡(ip -o link show); 重启后内核计数归零, 快照脚本自动续记
IFACE="${1:-eth0}"
SNAP_FILE="${2:-/root/.traffic_history}"
echo "$(date +%s) $(cat /sys/class/net/${IFACE}/statistics/tx_bytes 2>/dev/null || echo 0) $(cat /sys/class/net/${IFACE}/statistics/rx_bytes 2>/dev/null || echo 0)" >> "$SNAP_FILE"
