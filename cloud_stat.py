#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端归档统计(官方 Open API 版), 供 daily_report.sh 调用

为什么不用 rclone: 归档链路早已改走阿里云盘官方 Open API(个人/资源库),
旧的 rclone remote 配置早已失效, 用 `rclone lsf` 会 100% 报 not found。

用法: python3 cloud_stat.py 20261019 20261020 20261021
输出(每个日期一行, 便于 shell 直接拼日报):
    <day> <文件数> <小时合并数> <总MB>
云端无该日期目录时输出: <day> MISSING
token 失效时输出 `TOKEN_INVALID` 并以退出码 42 结束, 交给 run_merge_upload.sh 刷新
"""
import json
import sys
import urllib.error
import urllib.request

BASE = 'https://openapi.alipan.com/adrive/v1.0'
DRIVE_ID = 'YOUR_RESOURCE_DRIVE_ID'   # 与 merge_upload.py 保持一致
ROOT_PARENT = 'YOUR_ROOT_PARENT_FILE_ID'  # easynvr_rec/<CH> 的 file_id
TOKEN_FILE = '/opt/easynvr-cloud-archive/resource_token.json'


def api(path, data, token):
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print('TOKEN_INVALID', flush=True)
            sys.exit(42)
        return {}


def list_dir(token, parent):
    items, marker = [], None
    while True:
        d = {'drive_id': DRIVE_ID, 'parent_file_id': parent, 'limit': 100}
        if marker:
            d['marker'] = marker
        r = api('/openFile/list', d, token)
        items.extend(r.get('items', []))
        marker = r.get('next_marker')
        if not marker:
            break
    return items


def main():
    days = sys.argv[1:]
    if not days:
        print('usage: cloud_stat.py YYYYMMDD [...]', flush=True)
        return 1
    token = json.load(open(TOKEN_FILE))['access_token']
    dirs = {it['name']: it['file_id'] for it in list_dir(token, ROOT_PARENT) if it['type'] == 'folder'}
    for day in days:
        if day not in dirs:
            print('%s MISSING' % day)
            continue
        files = [it for it in list_dir(token, dirs[day]) if it['type'] == 'file']
        hours = [f for f in files if f['name'].endswith('.mp4') and f['name'].count('_') == 1]
        mb = sum(int(f.get('size', 0)) for f in files) / 1048576.0
        print('%s %d %d %.0f' % (day, len(files), len(hours), mb))
    return 0


if __name__ == '__main__':
    sys.exit(main())
