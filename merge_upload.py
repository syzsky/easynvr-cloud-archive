#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyNVR 分钟分片 → 按小时无损合并 → 上传阿里云盘资源库 → 云端比对 → 删除本地原始分片

用法:
    python3 merge_upload.py                 # 上传窗口内自动处理「已封盘」日期
    python3 merge_upload.py 20261019         # 显式指定日期(不受窗口限制, 便于补传)
    FORCE=1 python3 merge_upload.py          # 强制忽略上传窗口
    OUT_OF_WINDOW_DRYRUN=1 python3 ...       # 只看窗口判定, 不干活

策略(生产环境验证):
- 上传窗口默认 02:00-04:00(本地时区), 避开白天在线预览带宽; 显式传日期或 FORCE=1 可跳过
- 无参时只处理 d < 今天 且 早于本地保留窗口(KEEP_LOCAL_DAYS=2, 保留今天+昨天)的日期
- 合并前要求该小时末片 mtime 静置 >= SETTLE_SEC(默认 180s), 防止把正在写入的分片合进 mp4 造成截断
- 云端同名但 size 不一致 → 先把云端旧文件移入回收站(可恢复)再重传覆盖, 实现自愈
- 比对通过才删除本地分片与合并中间文件

输出:
- 合并: /data/easynvr/r/easynvr_rec/merged/<day>_<HH>.mp4
- 云端: 资源库 easynvr_rec/<CH>/<day>/<day>_<HH>.mp4
"""
import datetime
import glob
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = 'https://openapi.alipan.com/adrive/v1.0'
DRIVE_ID = 'YOUR_RESOURCE_DRIVE_ID'  # 替换为你的阿里云盘资源库 resource_drive_id (getDriveInfo 返回)
CH = 'YOUR_CHANNEL_CODE'  # 替换为 EasyNVR 通道编码
ROOT_PARENT = 'YOUR_ROOT_PARENT_FILE_ID'  # 替换为资源库目标文件夹 file_id (easynvr_rec/CH)
LOCAL_BASE = '/data/easynvr/r/easynvr_rec/%s/%s' % (CH, CH)
MERGED_DIR = '/data/easynvr/r/easynvr_rec/merged'
TOKEN_FILE = '/opt/easynvr-cloud-archive/resource_token.json'
PART_SIZE = 1024 * 1024
FFMPEG = '/usr/bin/ffmpeg'

# --- 调度策略参数(可用环境变量覆盖) ---
WIN_START = int(os.environ.get('UPLOAD_WIN_START_HOUR', '2'))   # 02:00 起
WIN_END = int(os.environ.get('UPLOAD_WIN_END_HOUR', '4'))       # 04:00 止(不含)
SETTLE_SEC = int(os.environ.get('SETTLE_SEC', '180'))           # 末片静置秒数
KEEP_LOCAL_DAYS = int(os.environ.get('KEEP_LOCAL_DAYS', '2'))   # 本地保留 2 个自然日(今天+昨天)


def now():
    return datetime.datetime.now()


def in_upload_window(ts=None):
    """默认 02:00-04:00 为上传窗口; 支持跨零点配置。"""
    if os.environ.get('FORCE') == '1' or os.environ.get('FORCE_RUN') == '1':
        return True
    h = (ts or now()).hour
    if WIN_START <= WIN_END:
        return WIN_START <= h < WIN_END
    return h >= WIN_START or h < WIN_END


def load_token():
    with open(TOKEN_FILE) as f:
        return json.load(f)


def api(path, data, token, method='POST'):
    req = urllib.request.Request(BASE + path,
        data=json.dumps(data).encode(), method=method,
        headers={'Authorization': 'Bearer ' + token,
                 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        if e.code == 401:
            print('TOKEN_EXPIRED 401', flush=True)
            sys.exit(42)
        print('HTTP %s: %s' % (e.code, body), flush=True)
        raise


def create_folder(token, parent, name):
    return api('/openFile/create', {
        'drive_id': DRIVE_ID, 'parent_file_id': parent,
        'name': name, 'type': 'folder', 'check_name_mode': 'refuse'
    }, token)


def list_dir(token, parent):
    items = []
    marker = None
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


def drop_cloud_file(token, file_id):
    """把云端同名旧文件移入回收站(可恢复), 用于尺寸不一致时删旧重传。"""
    for path in ('/openFile/recycle', '/openFile/trash', '/openFile/delete'):
        try:
            api(path, {'drive_id': DRIVE_ID, 'file_id': file_id}, token)
            return True
        except Exception:  # 接口不存在/参数不符时换下一个
            continue
    return False


def upload_file(token, parent, path, name):
    size = os.path.getsize(path)
    nparts = max(1, math.ceil(size / PART_SIZE))
    cr = api('/openFile/create', {
        'drive_id': DRIVE_ID, 'parent_file_id': parent,
        'name': name, 'type': 'file', 'check_name_mode': 'refuse',
        'size': size, 'part_size': PART_SIZE,
        'part_info_list': [{'part_number': i + 1} for i in range(nparts)]
    }, token)
    if cr.get('exist'):
        return 'exist', cr.get('file_id')
    file_id = cr['file_id']
    upload_id = cr['upload_id']
    part_infos = sorted(cr['part_info_list'], key=lambda x: x['part_number'])
    with open(path, 'rb') as f:
        for pi in part_infos:
            pn = pi['part_number']
            f.seek((pn - 1) * PART_SIZE)
            data = f.read() if pn == nparts else f.read(PART_SIZE)
            # OSS 签名 URL 要求 Content-Type 为空，urllib 会自动加导致 403，必须用 curl 显式清空
            p = subprocess.Popen(
                ['curl', '-sS', '-X', 'PUT', '--data-binary', '@-',
                 '-H', 'Content-Type:', '-m', '120', pi['upload_url']],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = p.communicate(data)
            if p.returncode != 0:
                print('  part %d FAILED: %s %s' % (pn, out.decode(errors='ignore')[:200],
                                                    err.decode(errors='ignore')[:200]), flush=True)
                raise RuntimeError('part upload failed')
            print('  part %d/%d done' % (pn, nparts), flush=True)
    done = api('/openFile/complete', {
        'drive_id': DRIVE_ID, 'file_id': file_id, 'upload_id': upload_id
    }, token)
    return 'ok', done.get('file_id')


def merge_day(day, day_dir):
    """按小时合并分片, 返回 [(hour, merged_path, nfrag)]"""
    today = now().strftime('%Y%m%d')
    now_hh = now().strftime('%H')
    files = sorted([f for f in os.listdir(day_dir) if f.endswith('.mp4')])
    groups = {}
    for fn in files:
        hh = fn[8:10]
        groups.setdefault(hh, []).append(os.path.join(day_dir, fn))
    merged = []
    os.makedirs(MERGED_DIR, exist_ok=True)
    for hh in sorted(groups):
        out = os.path.join(MERGED_DIR, '%s_%s.mp4' % (day, hh))
        if os.path.exists(out):
            merged.append((hh, out, len(groups[hh])))
            continue
        if day == today and hh >= now_hh:
            continue  # 正在写入的小时不合并
        # 末片静置检查: 防止把仍在写入的分片合进产物(截断/残缺)
        last_mtime = max(os.path.getmtime(p) for p in groups[hh])
        idle = time.time() - last_mtime
        if idle < SETTLE_SEC:
            print('SKIP %s_%s: 末片静置 %ds < %ds, 下一轮再合并'
                  % (day, hh, idle, SETTLE_SEC), flush=True)
            continue
        lst = os.path.join(MERGED_DIR, '%s_%s.list' % (day, hh))
        with open(lst, 'w') as f:
            for p in groups[hh]:
                f.write("file '%s'\n" % os.path.abspath(p))
        cmd = [FFMPEG, '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', out]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            print('MERGE FAIL %s_%s: %s' % (day, hh, r.stderr.decode(errors='ignore')[-300:]), flush=True)
            if os.path.exists(out):
                os.remove(out)
            continue
        print('merged %s_%s (%d frags, %.0f MB)'
              % (day, hh, len(groups[hh]), os.path.getsize(out) / 1048576), flush=True)
        merged.append((hh, out, len(groups[hh])))
    return merged


def main():
    today = now().strftime('%Y%m%d')
    explicit_days = sys.argv[1:]

    if os.environ.get('OUT_OF_WINDOW_DRYRUN') == '1':
        print('IN_WINDOW=%s (win %02d:00-%02d:00)' % (in_upload_window(), WIN_START, WIN_END), flush=True)
        return 0
    # 无参 + 窗口外 → 零动作退出(不读 token, 不打云盘)
    if not explicit_days and not in_upload_window():
        print('OUT_OF_WINDOW %s: 当前 %02d 时不在上传窗口 %02d:00-%02d:00(显式传日期或 FORCE=1 可跳过)'
              % (now().strftime('%F %T'), now().hour, WIN_START, WIN_END), flush=True)
        return 0

    token = load_token()['access_token']
    days = sorted([d for d in os.listdir(LOCAL_BASE)
                   if os.path.isdir(os.path.join(LOCAL_BASE, d)) and d.isdigit()])
    if explicit_days:
        days = [d for d in days if d in explicit_days]
    else:
        # 只处理已封盘日期: d < 今天, 且早于本地保留窗口
        # KEEP_LOCAL_DAYS 表示「含今天共保留几个自然日」, 2 → 保留今天+昨天, 归档昨天的前一天及更早
        cutoff = (now() - datetime.timedelta(days=max(1, KEEP_LOCAL_DAYS - 1))).strftime('%Y%m%d')
        days = [d for d in days if d < today and d <= cutoff]

    if not days:
        print('NO_DAY: 无已封盘待归档日期(window=%s-%s, keep=%dd)'
              % (WIN_START, WIN_END, KEEP_LOCAL_DAYS), flush=True)
        print('DONE', flush=True)
        return 0

    remote_dirs = {it['name']: it['file_id'] for it in list_dir(token, ROOT_PARENT) if it['type'] == 'folder'}
    for day in days:
        day_dir = os.path.join(LOCAL_BASE, day)
        merged = merge_day(day, day_dir)
        if not merged:
            print('== %s: 无待合并文件' % day, flush=True)
            continue
        if day not in remote_dirs:
            r = create_folder(token, ROOT_PARENT, day)
            remote_dirs[day] = r['file_id']
        parent = remote_dirs[day]
        existing = {it['name']: it for it in list_dir(token, parent) if it['type'] == 'file'}
        ok_hours = []
        for hh, out, nfrag in merged:
            name = '%s_%s.mp4' % (day, hh)
            lsize = os.path.getsize(out)
            if name in existing and existing[name].get('size') == lsize:
                print('== %s 云端已存在且大小一致, 跳过' % name, flush=True)
                ok_hours.append(name)
                continue
            if name in existing:
                # 云端残缺/旧版本 → 删旧(移回收站, 可恢复)后重传, 实现自愈
                print('!! %s 云端同名但大小不同(本地=%d 云端=%s), 删旧重传'
                      % (name, lsize, existing[name].get('size')), flush=True)
                if not drop_cloud_file(token, existing[name]['file_id']):
                    print('!! %s 云端旧文件回收失败, 保留本地待下轮重试' % name, flush=True)
                    continue
                existing.pop(name, None)
            print('上传 %s (%.0f MB)' % (name, lsize / 1048576), flush=True)
            try:
                st, fid = upload_file(token, parent, out, name)
            except Exception as e:
                print('!! %s 上传异常: %s, 保留本地' % (name, e), flush=True)
                continue
            if st not in ('ok', 'exist') or not fid:
                print('!! %s 上传失败, 保留本地' % name, flush=True)
                continue
            r = api('/openFile/get', {'drive_id': DRIVE_ID, 'file_id': fid}, token)
            rsize = r.get('size')
            if rsize == lsize:
                ok_hours.append(name)
                existing[name] = r
                print('== %s 比对通过 (size=%d)' % (name, rsize), flush=True)
            else:
                print('!! %s 比对失败 本地=%d 云端=%s' % (name, lsize, rsize), flush=True)
        if ok_hours:
            deleted = 0
            for hh, out, nfrag in merged:
                name = '%s_%s.mp4' % (day, hh)
                if name in ok_hours:
                    for p in glob.glob(os.path.join(day_dir, '%s%s*.mp4' % (day, hh))):
                        try:
                            os.remove(p)
                            deleted += 1
                        except OSError as e:
                            print('!! 删除失败 %s: %s' % (p, e), flush=True)
                    if os.path.exists(out):
                        os.remove(out)
            print('== %s 已删本地 %d 个分片' % (day, deleted), flush=True)
    print('DONE', flush=True)


if __name__ == '__main__':
    main()
