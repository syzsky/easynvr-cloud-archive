#!/bin/bash
# =============================================================================
# daily_report.sh: 每日巡检日报推送到微信(Server酱) / 其它渠道自行替换
#
# 内容: 服务器状态(重启标记) + 今日流量(出/入) + 磁盘 + 本地保留
#       + 云盘近三日(官方 API 取数) + 云盘 token 健康 + 昨日归档 + 异常记录
#
# 依赖: curl, python3, 网卡快照(traffic_snapshot.sh 每小时一次)
#
# cron(每天 09:00):
#   0 9 * * * /opt/easynvr-cloud-archive/daily_report.sh
#
# 2026-10 取数修复(此前三处全空):
#   1) 归档日志: 旧路径 /var/log/easynvr_cloud_archive.log 已于 10-17 停写,
#      实际写日志的是 merge_upload 链路 → /var/log/merge_upload.log
#   2) 云盘: 废弃 rclone 旧 remote(配置早失效, 必 not found), 改走官方 Open API(cloud_stat.py)
#   3) 流量: 快照文件为空时不再静默显示空值, 明确提示快照缺失, 便于发现 cron 未生效
#
# 注意: SEND_KEY 换成自己的 Server酱 SendKey, 不要提交真实 key 到 git
# =============================================================================
SEND_KEY="YOUR_SERVERCHAN_SENDKEY"     # https://sct.ftqq.com 获取(GitHub登录)
SNAP_FILE="/root/.traffic_history"     # 与 traffic_snapshot.sh 保持一致
SYNC_LOG="/var/log/merge_upload.log"   # 实际归档日志(合并/上传/删除都写这里)
SYNC_LOG_OLD="/var/log/easynvr_cloud_archive.log"  # 旧日志, 仅作兜底
LOCAL_BASE="/data/easynvr/r/easynvr_rec/34020000001320000001/34020000001320000001"
MOUNT_POINT="/data"
CLOUD_STAT="/opt/easynvr-cloud-archive/cloud_stat.py"
TOKEN_STATE="/opt/easynvr-cloud-archive/.token_state.json"

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

# --- 云盘近三日: 文件数 + 小时合并数 + 体积(官方 API) ---
CLOUD=""
if [ -f "$CLOUD_STAT" ]; then
  C_OUT=$(python3 "$CLOUD_STAT" $(date +%Y%m%d) $(date -d yesterday +%Y%m%d) $(date -d "2 days ago" +%Y%m%d) 2>/dev/null)
  while read -r d n h mb; do
    [ -z "$d" ] && continue
    if [ "$n" = "MISSING" ] || [ "$n" = "TOKEN_INVALID" ]; then
      CLOUD="${CLOUD}- ${d}: 无目录/取数失败(${n})\n"
    else
      CLOUD="${CLOUD}- ${d}: ${n} 个文件, 小时合并 ${h} 个, ${mb}MB\n"
    fi
  done <<< "$C_OUT"
else
  CLOUD="- cloud_stat.py 未部署, 云盘取数跳过\n"
fi

# --- 云盘 token 健康: 剩余有效期 + 刷新失败次数 ---
TOK_LINE=$(python3 - "$TOKEN_STATE" <<'PY'
import json, sys, time
st = {}
try:
    st = json.load(open(sys.argv[1]))
except Exception:
    pass
exp = int(st.get('exp', 0))
left = exp - int(time.time())
fc = int(st.get('fail_count', 0))
src = st.get('source', '-')
if left <= 0:
    print('已过期或未知(exp 缺失), 刷新失败次数 %d' % fc)
elif left > 3600:
    print('有效, 剩余 %.1f 小时, 刷新失败次数 %d, 最近来源 %s' % (left / 3600.0, fc, src))
else:
    print('有效, 剩余 %d 分钟, 刷新失败次数 %d, 最近来源 %s' % (left // 60, fc, src))
PY
)

# --- 当日流量(出/入), 依赖每小时快照 ---
D0=$(date -d "today 00:00" +%s)
if [ ! -s "$SNAP_FILE" ]; then
  TR="快照缺失(检查 traffic_snapshot.sh 的 cron 是否生效)"
else
  CNT=$(awk -v t0="$D0" '$1 >= t0' "$SNAP_FILE" | wc -l)
  if [ "$CNT" -lt 1 ]; then
    TR="今日尚无快照(最近一条: $(tail -1 "$SNAP_FILE" | awk '{print strftime("%F %H:%M", $1)}'))"
  else
    TRVAL=$(awk -v t0="$D0" '$1 >= t0 { if(!f){tx0=$2;rx0=$3;f=1} tx=$2; rx=$3 } END { if(f) printf "%.2fG / %.2fG", (tx-tx0)/1073741824, (rx-rx0)/1073741824; else print "- / -" }' "$SNAP_FILE")
    TR="$TRVAL"
  fi
fi

# --- 归档 & 异常(新日志优先, 旧日志兜底) ---
TODAY=$(date +%F)
ARCH=$(grep "$TODAY" "$SYNC_LOG" 2>/dev/null | grep -E '已删本地|已归档并删除本地|比对通过' | tail -3)
ERR=$(grep "$TODAY" "$SYNC_LOG" 2>/dev/null | grep -iE '失败|FAIL|ERROR|比对失败' | tail -3)
if [ -z "$ARCH" ] && [ -f "$SYNC_LOG_OLD" ]; then
  ARCH=$(grep "$TODAY" "$SYNC_LOG_OLD" 2>/dev/null | grep '已归档并删除本地' | tail -2)
fi

DESP="**巡检时间**: $(date '+%F %T')

**服务器状态**: ${UP_NOTE}
**今日流量(出/入)**: ${TR}
**磁盘 ${MOUNT_POINT}**: 已用 ${USE}%
**本地录像保留**: ${LOCAL_DIRS:-无}

**云盘近三日**:
${CLOUD}**云盘 token**: ${TOK_LINE}
**今日归档**: ${ARCH:-今日暂无归档}
**异常记录**: ${ERR:-无}"

curl -s -X POST "https://sctapi.ftqq.com/${SEND_KEY}.send" \
  --data-urlencode "title=巡检日报 $(date '+%m-%d')" \
  --data-urlencode "desp=${DESP}" >/dev/null
