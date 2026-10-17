#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyNVR 分钟分片 → 按小时无损合并 → 上传阿里云盘资源库 → 云端对比 → 删除本地原始分片
用法: python3 merge_upload.py [日期...]   # 不传日期则处理全部 2026 日期目录
- 合并输出: /data/easynvr/r/easynvr_rec/merged/<day>_<HH>.mp4
- 上传目标: 资源库 easynvr_rec/<CH>/<day>/<day>_<HH>.mp4
- 正在写入的日期（今天）跳过当前小时及之后，只合并已完整小时
- 对比通过才删除本地分钟分片与合并中间文件
"""
import json, os, sys, math, time, glob, subprocess, datetime, urllib.request, urllib.error

BASE = 'https://openapi.alipan.com/adrive/v1.0'
DRIVE_ID = 'YOUR_RESOURCE_DRIVE_ID'  # 替换为你的阿里云盘资源库 resource_drive_id (getDriveInfo 返回)
CH = 'YOUR_CHANNEL_CODE'  # 替换为 EasyNVR 通道编码
ROOT_PARENT = 'YOUR_ROOT_PARENT_FILE_ID'  # 替换为资源库目标文件夹 file_id (easynvr_rec/CH)
LOCAL_BASE = '/data/easynvr/r/easynvr_rec/%s/%s' % (CH, CH)
MERGED_DIR = '/data/easynvr/r/easynvr_rec/merged'
TOKEN_FILE = '/opt/easynvr-cloud-archive/resource_token.json'
PART_SIZE = 1024 * 1024
FFMPEG = '/usr/bin/ffmpeg'


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
        return 'exist', None
    file_id = cr['file_id']
    upload_id = cr['upload_id']
    part_infos = sorted(cr['part_info_list'], key=lambda x: x['part_number'])
    with open(path, 'rb') as f:
        for pi in part_infos:
            pn = pi['part_number']
            if pn == nparts:
                f.seek((pn - 1) * PART_SIZE)
                data = f.read()
            else:
                f.seek((pn - 1) * PART_SIZE)
                data = f.read(PART_SIZE)
            # OSS 签名 URL 要求 Content-Type 为空，urllib 会自动加导致 403，必须用 curl 显式清空
            p = subprocess.Popen(
                ['curl', '-sS', '-X', 'PUT', '--data-binary', '@-',
                 '-H', 'Content-Type:', '-m', '120', pi['upload_url']],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = p.communicate(data)
            if p.returncode != 0:
                print('  part %d FAILED: %s %s' % (pn, out.decode(errors='ignore')[:200], err.decode(errors='ignore')[:200]), flush=True)
                raise RuntimeError('part upload failed')
            print('  part %d/%d done' % (pn, nparts), flush=True)
    done = api('/openFile/complete', {
        'drive_id': DRIVE_ID, 'file_id': file_id, 'upload_id': upload_id
    }, token)
    return 'ok', done.get('file_id')


def merge_day(day, day_dir):
    """按小时合并分片，返回 [(hour, merged_path, nfrag)]"""
    today = datetime.datetime.now().strftime('%Y%m%d')
    now_hh = datetime.datetime.now().strftime('%H')
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
        lst = os.path.join(MERGED_DIR, '%s_%s.list' % (day, hh))
        with open(lst, 'w') as f:
            for p in groups[hh]:
                f.write("file '%s'\n" % p)
        cmd = [FFMPEG, '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', out]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            print('MERGE FAIL %s_%s: %s' % (day, hh, r.stderr.decode(errors='ignore')[-300:]), flush=True)
            if os.path.exists(out):
                os.remove(out)
            continue
        print('merged %s_%s (%d frags, %.0f MB)' % (day, hh, len(groups[hh]), os.path.getsize(out) / 1048576), flush=True)
        merged.append((hh, out, len(groups[hh])))
    return merged


def main():
    token = load_token()['access_token']
    target_days = sys.argv[1:] if len(sys.argv) > 1 else None
    days = sorted([d for d in os.listdir(LOCAL_BASE)
                   if os.path.isdir(os.path.join(LOCAL_BASE, d)) and d.startswith('2026')])
    if target_days:
        days = [d for d in days if d in target_days]
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
        existing = {it['name']: it['size'] for it in list_dir(token, parent) if it['type'] == 'file'}
        ok_hours = []
        for hh, out, nfrag in merged:
            name = '%s_%s.mp4' % (day, hh)
            if name in existing and existing[name] == os.path.getsize(out):
                print('== %s 云端已存在且大小一致, 跳过' % name, flush=True)
                ok_hours.append(name)
                continue
            if name in existing:
                print('!! %s 云端同名但大小不同(本地=%d 云端=%d), 保留本地' % (name, os.path.getsize(out), existing[name]), flush=True)
                continue
            print('上传 %s (%.0f MB)' % (name, os.path.getsize(out) / 1048576), flush=True)
            st, fid = upload_file(token, parent, out, name)
            if st != 'ok':
                print('!! %s 上传失败, 保留本地' % name, flush=True)
                continue
            r = api('/openFile/get', {'drive_id': DRIVE_ID, 'file_id': fid}, token)
            rsize = r.get('size')
            if rsize == os.path.getsize(out):
                ok_hours.append(name)
                print('== %s 对比通过 (size=%d)' % (name, rsize), flush=True)
            else:
                print('!! %s 对比失败 本地=%d 云端=%s' % (name, os.path.getsize(out), rsize), flush=True)
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
