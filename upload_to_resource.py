#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传 EasyNVR 本地录像到阿里云盘【资源库】(drive_type=resource) 直传脚本
- 走官方 Open API openapi.alipan.com（国内可达）
- token 从 /opt/easynvr-cloud-archive/resource_token.json 读取（access_token 2h 有效）
- 支持断点续传：按日期目录 list 已有文件，重名跳过
- 401 时退出码 42，由外部刷新 token 后续传
"""
import json, os, sys, math, time, urllib.request, urllib.error, subprocess

BASE = 'https://openapi.alipan.com/adrive/v1.0'
DRIVE_ID = 'YOUR_RESOURCE_DRIVE_ID'  # 替换为你的阿里云盘资源库 resource_drive_id (getDriveInfo 返回)
CH = 'YOUR_CHANNEL_CODE'  # 替换为 EasyNVR 通道编码
ROOT_PARENT = 'YOUR_ROOT_PARENT_FILE_ID'  # 替换为资源库目标文件夹 file_id (easynvr_rec/CH)
LOCAL_BASE = '/data/easynvr/r/easynvr_rec/%s/%s' % (CH, CH)
TOKEN_FILE = '/opt/easynvr-cloud-archive/resource_token.json'
PART_SIZE = 1024 * 1024  # 1MB，必须为 1MB 整数倍

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

def main():
    token = load_token()['access_token']
    dirs = sorted([d for d in os.listdir(LOCAL_BASE)
                   if os.path.isdir(os.path.join(LOCAL_BASE, d)) and d.startswith('2026')])
    print('本地日期目录: %s' % dirs, flush=True)
    remote_children = list_dir(token, ROOT_PARENT)
    remote_dirs = {it['name']: it['file_id'] for it in remote_children if it['type'] == 'folder'}
    total_ok = total_skip = 0
    for day in dirs:
        if day not in remote_dirs:
            r = create_folder(token, ROOT_PARENT, day)
            remote_dirs[day] = r['file_id']
            print('创建远端日期目录 %s' % day, flush=True)
        parent = remote_dirs[day]
        existing = {it['name'] for it in list_dir(token, parent) if it['type'] == 'file'}
        files = sorted(os.listdir(os.path.join(LOCAL_BASE, day)))
        print('== %s: 本地 %d 个文件, 远端已有 %d 个' % (day, len(files), len(existing)), flush=True)
        for fn in files:
            if fn in existing:
                total_skip += 1
                continue
            fp = os.path.join(LOCAL_BASE, day, fn)
            if not os.path.isfile(fp):
                continue
            print('上传 %s/%s (%d MB)' % (day, fn, os.path.getsize(fp) // 1048576), flush=True)
            st, fid = upload_file(token, parent, fp, fn)
            if st == 'exist':
                total_skip += 1
            elif st == 'ok':
                total_ok += 1
            else:
                print('未知状态 %s' % st, flush=True)
    print('DONE total_ok=%d total_skip=%d' % (total_ok, total_skip), flush=True)

if __name__ == '__main__':
    main()
