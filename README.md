# easynvr-cloud-archive

EasyNVR 录像「本地碎分片 → 按小时无损合并 → 云盘归档」的完整运维方案，附带每日巡检日报与存活监控模板。

解决真实痛点：EasyNVR 默认把录像切成约 64 秒的碎分片存在本地，**只进不出**，小容量服务器几天就会把磁盘写满。本项目把「足够老」的录像按小时无损合并成 `YYYYMMDD_HH.mp4` 上传到任意 rclone 支持的云盘，**上传校验通过后才删除本地**，并保留最近 N 天在本地供 NVR 平台近端回放。

> 本仓库为通用化/去敏版本，由真实生产环境（31G 磁盘、日增约 7G 录像、10Mbps 上行限 8Mbps、跨容器挂载点等约束）演进而来，踩坑记录见文末 FAQ。

---

## 架构

```
 EasyNVR 录像写入 (~64s 碎分片, 本地目录 /data/easynvr/r/...)
        │
        ▼
 ┌───────────────────────────── cron 每 5 分钟 ─────────────────────────────┐
 │ easynvr_cloud_archive.sh                                                 │
 │   ① 磁盘水位/窗口判定:                                                     │
 │      凌晨 02:00-04:30 窗口执行(窗口制)                                     │
 │      磁盘 >=90% 任意时刻执行(紧急制)                                        │
 │      窗口外且未超水位 → 零动作退出                                          │
 │   ② 找出早于保护期(D-2 及更早, PROTECT_N=2)的日期目录                        │
 │   ③ 逐小时 ffmpeg concat -c copy 无损合并碎分片 → YYYYMMDD_HH.mp4           │
 │   ④ rclone --bwlimit 900K 上传到云盘                                        │
 │   ⑤ 校验远端大小 == 本地大小 → 清理云盘残留碎分片 → 删除本地日期目录            │
 └──────────────────────────────────────────────────────────────────────────┘
        │
        ▼
 云盘: aliyun:/aliyun/easynvr_rec/<CH>/<YYYYMMDD>/<YYYYMMDD>_HH.mp4
 本地: 仅保留今天+昨天(D-1), 供 EasyNVR 近端回放
```

## 目录结构

```
easynvr-cloud-archive/
├── easynvr_cloud_archive.sh    # 主脚本: 合并+上传+校验+清理
├── config.example.conf         # 配置模板(复制为 config.conf 使用)
├── daily_report.example.sh     # 每日巡检日报 → Server酱(微信)推送
├── monitor.example.sh          # 存活监控(宕机告警, 需放独立主机)
├── traffic_snapshot.sh         # 网卡流量每小时快照(日报的流量数据源)
└── README.md
```

## 快速开始

### 1. 准备依赖

- `ffmpeg`：用于无损合并（`apt install ffmpeg` / `yum install ffmpeg`）
- `rclone`：云盘上传（官方脚本安装）
- （可选）`curl`：日报/告警推送

### 2. 配置 rclone 云盘

```bash
rclone config          # 新增 remote, 如 aliyun:(阿里云盘 WebDAV 走 alist)
rclone lsd aliyun:     # 验证可读
```

### 3. 准备配置

```bash
cp config.example.conf config.conf
vim config.conf
# 至少改: CH / LOCAL_BASE / DST_CH / MOUNT_POINT
# 按需改: BANDWIDTH_LIMIT / PROTECT_N / 窗口与水位
```

### 4. 试跑（推荐先空跑 + 单小时验证）

```bash
# 直接前台跑一次看日志输出(非窗口且磁盘不紧张时会零动作退出)
bash easynvr_cloud_archive.sh

# 手动强制跑一次(仍遵守保护期与限速)
FORCE_RUN=1 bash easynvr_cloud_archive.sh

# 小规模验证: 建议先用某个「足够老」的日期目录试跑, 确认:
#   - 云盘出现 <日期>_HH.mp4
#   - 日志出现 "已归档并删除本地 <日期>, 并清理云盘 <日期> 碎分片"
#   - 本地该日期目录消失, 云盘文件可正常播放
```

### 5. 配置 cron（每 5 分钟调度）

```bash
crontab -e
# 归档: 每 5 分钟跑一次(脚本内部自行判断窗口/水位, 非执行时机零动作退出)
*/5 * * * * EASYNVR_CFG=/opt/easynvr-cloud-archive/config.conf bash /opt/easynvr-cloud-archive/easynvr_cloud_archive.sh
```

## 调度与保留策略

| 参数 | 默认 | 说明 |
|---|---|---|
| `WINDOW_START_MIN` / `WINDOW_END_MIN` | 120 / 270 | 每天凌晨 02:00-04:30 自动归档 |
| `URGENT_TRIGGER_PCT` / `URGENT_STOP_PCT` | 90 / 80 | 磁盘 ≥90% 立即归档, 降到 80% 停 |
| `PROTECT_N` | 2 | 本地保留最近 2 天(今天+昨天)供近端回放 |
| `BANDWIDTH_LIMIT` | 900K | rclone 限速(注意单位是 Byte/s) |
| `FORCE_RUN=1` | 手动 | 窗口外强制执行一次 |
| `ARCHIVE_YDAY=1` | 手动 | 连昨天一起归档(临时把 PROTECT_N 降为 1, 慎用) |

cron 频率建议 5 分钟；脚本有 flock 单实例锁，重复调度不会并发。

## 日报与告警（可选）

```bash
# 1) 网卡流量快照: 每小时记一次(日报的「今日流量」依赖它)
#    crontab: 1 * * * * /opt/easynvr-cloud-archive/traffic_snapshot.sh eth0 /root/.traffic_history

# 2) 每日巡检日报 → 微信: 改 daily_report.example.sh 里的 SEND_KEY, 每天 09:00
#    crontab: 0 9 * * * /opt/easynvr-cloud-archive/daily_report.sh
#    日报含: 服务器状态(当天重启会标注) / 今日出网·入网 / 磁盘 / 本地保留 / 云盘近三日 / 归档结果 / 异常

# 3) 存活监控(宕机告警): 复制到「独立于被监控机的另一台常开主机」
#    crontab: */5 * * * * /opt/monitor/monitor.sh
#    连续 3 次(约 15 分钟)探测失败推送「疑似宕机」, 恢复后推送「已恢复」
```

Server酱 SendKey 获取：https://sct.ftqq.com（GitHub 登录，免费版每日 5 条够日报+告警）。替换成钉钉/飞书/企业微信机器人只改最后的 curl 即可。

## 设计决策（为什么这么做）

1. **按小时合并，不按天合并**：单文件体积可控（1 小时 ≈ 150-750MB），上传断点损失小；回放可按小时定位；concat list 都是同一天内分片，格式参数一致，可 `-c copy` 零转码。
2. **先上传校验，后删本地**：上传后用 `rclone lsl` 比对远端字节数与本地一致才删除，杜绝「传一半/传失败却删了本地」导致丢录像。
3. **保留近端 D-1 + 今天**：EasyNVR 平台回放是实时扫描本地 mp4 目录，删了老日期只缩短回放窗口，不会报错；保留 2 天是「回放可用性」与「磁盘空间」的平衡点。
4. **窗口制 + 紧急水位双触发**：白天上传会影响在线预览带宽，所以默认只在凌晨窗口干活；磁盘快满时打破窗口兜底，防止写满导致录像中断。
5. **限速 900K ≈ 7.4Mbps**：见 FAQ「带宽换算坑」。
6. **concat 必须绝对路径**：ffmpeg 读 concat list 时相对路径基于其工作目录，不在录像目录执行就会「找不到源文件」，list 里必须写绝对路径。

## FAQ / 踩坑记录

### Q1: rclone `--bwlimit 900K` 到底多快？
rclone 的 `K` 是 **KiB/s（字节）**：900K ≈ 0.88 MiB/s ≈ **7.4 Mbps(bit)**。很多人设 `2M` 以为才 2Mbps，实际是 **16.8Mbps**，已经超了普通 10M 上行带宽的可用额度。带宽受限场景务必先换算：`rclone 限速 X K → 实际 X*8/1000 Mbps`。

### Q2: 为什么合并前要确认相邻分片参数一致？
`-c copy` 不转码，若相邻分片编码参数不一致（分辨率/帧率/音轨）会花屏或失败。EasyNVR 同一天同通道参数一致，实测 64s 分片可无损拼回。跨通道/跨天混拼前先 `ffprobe` 对比。

### Q3: 上传中途失败怎么办？
脚本只有「该校小时全部合并上传校验成功」才删本地日期目录；任一小时失败该日期整体保留，下一轮 cron 重试（`merge_one_hour` 已有产物会跳过，天然断点续传）。

### Q4: 本地目录结构为什么是双层通道名？
EasyNVR 实际录像路径形如 `r/easynvr_rec/<CH>/<CH>/<YYYYMMDD>/*.mp4`（第一层为配置目录、第二层为通道目录），**先 `ls` 确认再填 `LOCAL_BASE`**，填错会「找不到目录」空跑。

### Q5: 云盘里残留一堆旧碎分片怎么来的？会清吗？
若早期版本把碎分片直接传上云，云盘会混存碎分片 + 合并文件。本脚本在归档某日后会 `rclone delete <云端该日期> --include '*-64000.mp4'` 清理该日期残留碎文件（仅删除已合并进 `*_HH.mp4` 的碎片，不影响合并文件）。首次接入存量云盘前建议先人工核对清理范围。

### Q6: Docker/NVR 容器里 rename 报 `Invalid cross-device link`？
容器内每个 `-v` 卷是独立挂载点（`/proc/self/mountinfo` 可查），**跨挂载点 `mv/rename` 一律 EXDEV**。归档/合并临时目录务必与被处理目录放同一挂载点内（本项目 `MERGE_TMP_DIR` 默认放 `/var/tmp`，若跨盘请改到录像同盘），或改用 `cp+rm`。用 `strace` 可快速抓到真实报错点。

### Q7: 服务器挂了日报还会发吗？
不会——日报由被监控机自己推送，只能反映「挂过又拉起」（日报会标注当天重启时间）。真正的宕机告警需要把 `monitor.sh` 放在另一台常开主机上探测。

### Q8: 多通道怎么办？
每通道一份 `config.conf`（改 `CH/LOCAL_BASE/DST_CH`），cron 里逐通道调用；注意限速按通道叠加，总带宽别超上行上限。

## 安全与合规提示

- 所有示例中的 SendKey / token 均为占位，请勿把真实凭据提交到公开仓库（可放 `config.conf` 并加入 `.gitignore`）
- 删除类操作（本地目录、云盘碎文件）脚本均放在「上传校验通过后」执行；首次使用仍建议先小规模验证再全量
- 按你的场景调整保留天数与水位阈值，确保满足业务合规与回放需求

## 部署服务

不想自己折腾？本方案作者提供远程部署服务：

- 远程部署（已有服务器/EasyNVR）：299 元/次
- 完整交付（含服务器/云盘选型 + 部署 + 回放验证 + 交付文档）：800 元起
- 月度运维巡检（微信日报 + 磁盘/宕机告警）：99 元/月

联系方式：YOUR_CONTACT_PLACEHOLDER（微信/邮箱/闲鱼链接，发布前替换）

## License

MIT（或按你的偏好修改）
