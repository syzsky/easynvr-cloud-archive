#!/bin/bash
# =============================================================================
# daily_report.sh 示例: 每日巡检日报推送到微信(Server酱) / 其它渠道自行替换
#
# 内容: 服务器状态(重启标记) + 今日流量(出/入) + 磁盘 + 本地保留 + 云盘情况
#       + 昨日归档 + 异常记录
#
# 依赖:
#   - curl
#   - 网卡快照: traffic_snapshot.sh(每小时记录一次 eth0 字节数, 见同目录)
#
# 配置 cron(每天 09:00):
#   0 9 * * * /opt/easynvr-cloud-archive/daily_report.sh
#
# 注意: 把下面的 SEND_KEY 换成你自己的 Server酱 SendKey, 不要提交真实 key 到 git
# =============================================================================
SEND_KEY="YOUR_SERVERCHAN_SENDKEY"     # https://sct.ftqq.com 获取(GitHub登录)
SNAP_FILE="/root/.traffic_history"     # 与 traffic_snapshot.sh 保持一致
SYNC_LOG="/var/log/easynvr_cloud_archive.log"
LOCAL_BASE="/data/easynvr/r/easynvr_rec/34020000001320000001/34020000001320000001"
CLOUD_BASE="aliyun:/aliyun/easynvr_rec/34020000001320000001"
MOUNT_POINT="/data"

[ "$SEND_KEY" = "YOUR_SERVERCHAN_SENDKEY" ] && { echo "请先配置 SEND_KEY"; exit 1; }

# --- 服务器运行状态(今天重启过 => 标注疑似宕机) ---
UP_SINCE=$(uptime -s)
if [ "${UP_SINCE:0:10}" = "$(date +%F)" ]; then
  UP_NOTE="今日 ${UP_SINCE:11:5} 曾重启/拉起(注意检查是否异常宕机)"
else
  UP_NOTE="运行中, 开机于 ${UP_SINCE}"
fi

# --- 磁盘 & 本地保留 ---
USE=$(df "$MOUNT_POINT" | awk 'NR==2 {gsub("%","",$5); print $5}')
LOCAL_DIRS=$(ls "$LOCAL_BASE" 2>/dev/null | grep -E '^[0-9]{8}$' | tr '\n' ' ')

# --- 云盘近三日: 文件数 + 小时合并文件数 ---
CLOUD=""
for d in $(date +%Y%m%d) $(date -d yesterday +%Y%m%d) $(date -d "2 days ago" +%Y%m%d); do
  n=$(rclone lsf "$CLOUD_BASE/$d" --max-depth 0 2>/dev/null | wc -l)
  h=$(rclone lsf "$CLOUD_BASE/$d" --max-depth 0 2>/dev/null | grep -cE "[0-9]{8}_[0-9]{2}\.mp4$")
  [ "$n" -gt 0 ] && CLOUD="$CLOUD- $d: ${n} 个文件, 小时合并 ${h} 个\n"
done

# --- 当日流量(出/入), 依赖每小时快照 ---
D0=$(date -d "today 00:00" +%s)
TR=$({ [ -f "$SNAP_FILE" ] && awk -v t0="$D0" '$1 >= t0 { if(!f){tx0=$2;rx0=$3;f=1} tx=$2; rx=$3 } END { if(f) printf "%.2fG %.2fG", (tx-tx0)/1073741824, (rx-rx0)/1073741824; else print "- -" }' "$SNAP_FILE"; })
TX=$(echo "$TR" | awk '{print $1}'); RX=$(echo "$TR" | awk '{print $2}')

# --- 归档 & 异常 ---
TODAY=$(date +%F)
ARCH=$(grep "$TODAY" "$SYNC_LOG" 2>/dev/null | grep '已归档并删除本地' | tail -2)
ERR=$(grep "$TODAY" "$SYNC_LOG" 2>/dev/null | grep -iE '失败|校验失败|ERROR' | tail -3)

DESP="**巡检时间**: $(date '+%F %T')

**服务器状态**: ${UP_NOTE}
**今日流量(出/入)**: ${TX} / ${RX}
**磁盘 ${MOUNT_POINT}**: 已用 ${USE}%
**本地录像保留**: ${LOCAL_DIRS:-无}

**云盘近三日**:
${CLOUD}**今日归档**: ${ARCH:-今日暂无归档}
**异常记录**: ${ERR:-无}"

curl -s -X POST "https://sctapi.ftqq.com/${SEND_KEY}.send" \
  --data-urlencode "title=巡检日报 $(date '+%m-%d')" \
  --data-urlencode "desp=${DESP}" >/dev/null
