#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿里云盘 token 自愈刷新(personal token 链路, 无需 client_secret)

背景: 第三方 Cloudflare 中继(alist 在线刷新服务)被网络封锁后经常 InvalidParameter,
      需要用「多候选 refresh_token + 官方 auth 端点 + 刷新后实测」的自愈链路。

自愈策略:
1. flock 单飞: 并发调用只有一个真正刷新, 其余直接退出 0(视为等待他人完成)
2. 候选链(去重后逐个尝试):
     personal_token.json → personal_token.json.prev → alist data.db(storage id=4 的 addition)
     → /root/.adrive_seed.json
3. 强制 IPv4: 只解析 A 记录, 规避 IPv6 黑洞导致的 hang
4. 刷新: POST https://auth.aliyundrive.com/v2/account/token  {grant_type, refresh_token}
5. 刷新后实测: 调 list/user 接口验证 access_token 真的可用; 遇 InvalidParameter/400 立即换下一候选
6. 成功: 旧文件备份为 .prev, 新 token 回写 alist data.db 与 seed, 保持同代, 并记录状态供日报使用

退出码: 0=有效或已刷新  2=所有候选均失败(需人工介入)
"""
import base64
import fcntl
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request

TOKEN_FILE = '/opt/easynvr-cloud-archive/personal_token.json'
PREV_FILE = TOKEN_FILE + '.prev'
ALIST_DB = '/opt/alist/data/data.db'
ALIST_STORAGE_ID = int(os.environ.get('ALIST_STORAGE_ID', '4'))
SEED_FILE = '/root/.adrive_seed.json'
STATE_FILE = '/opt/easynvr-cloud-archive/.token_state.json'
LOCK_FILE = '/var/lock/token_refresh.lock'

AUTH_URL = 'https://auth.aliyundrive.com/v2/account/token'
LIST_URL = 'https://api.aliyundrive.com/adrive/v3/file/list'
USER_URL = 'https://api.aliyundrive.com/v2/user/get'
VERIFY_DRIVE_ID = os.environ.get('VERIFY_DRIVE_ID', '')  # 可填资源库 drive_id, 留空则用 user/get 验证

CANARY = {
    'X-Canary': 'client=Android,app=adrive,version=v4.1.0',
    'User-Agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/110 Mobile Safari/537.36',
}


# ---------------- 基础工具 ----------------
def force_ipv4():
    """只返回 A 记录, 规避 IPv6 不可达导致的长时间挂起。"""
    orig = socket.getaddrinfo

    def gai(host, port, family=0, type=0, proto=0, flags=0):
        infos = orig(host, port, socket.AF_INET, type, proto, flags)
        return infos or orig(host, port, family, type, proto, flags)

    socket.getaddrinfo = gai


def b64url_decode(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def jwt_exp(tok):
    seg = (tok or '').split('.')
    if len(seg) != 3:
        return 0
    try:
        return int(json.loads(b64url_decode(seg[1]))['exp'])
    except Exception:
        return 0


def http_json(url, body, token=None, extra=None):
    headers = {'Content-Type': 'application/json'}
    headers.update(CANARY)
    if extra:
        headers.update(extra)
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors='ignore')[:300]
        try:
            d = json.loads(raw)
            d.setdefault('http_status', e.code)
            return d
        except Exception:
            return {'code': 'HTTP%d' % e.code, 'message': raw}
    except Exception as e:  # 超时/DNS/连接失败
        return {'code': 'NETWORK', 'message': str(e)[:200]}


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def write_state(**kw):
    st = read_json(STATE_FILE)
    st.update(kw)
    st['updated_at'] = int(time.time())
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(st, f)
    except Exception as e:
        print('state write failed: %s' % e, flush=True)


# ---------------- 候选链 ----------------
def candidates():
    """返回 [(来源, refresh_token)], 按优先级去重。"""
    out, seen = [], set()

    def add(src, rt):
        rt = (rt or '').strip()
        if not rt or rt in seen:
            return
        seen.add(rt)
        out.append((src, rt))

    add('personal_token.json', read_json(TOKEN_FILE).get('refresh_token'))
    add('personal_token.json.prev', read_json(PREV_FILE).get('refresh_token'))

    try:
        con = sqlite3.connect('file:%s?mode=ro' % ALIST_DB, uri=True, timeout=5)
        row = con.execute('SELECT addition FROM storages WHERE id=?', (ALIST_STORAGE_ID,)).fetchone()
        con.close()
        if row and row[0]:
            add('alist_db(id=%d)' % ALIST_STORAGE_ID, json.loads(row[0]).get('refresh_token'))
    except Exception as e:
        print('alist db read skip: %s' % e, flush=True)

    add('adrive_seed.json', read_json(SEED_FILE).get('refresh_token'))
    return out


def write_back_alist(access_token, refresh_token):
    if not os.path.exists(ALIST_DB):
        return
    try:
        con = sqlite3.connect(ALIST_DB, timeout=10)
        row = con.execute('SELECT addition FROM storages WHERE id=?', (ALIST_STORAGE_ID,)).fetchone()
        if not row:
            con.close()
            return
        add = json.loads(row[0] or '{}')
        add['refresh_token'] = refresh_token
        if 'access_token' in add:
            add['access_token'] = access_token
        con.execute('UPDATE storages SET addition=? WHERE id=?',
                    (json.dumps(add, ensure_ascii=False), ALIST_STORAGE_ID))
        con.commit()
        con.close()
        print('write back alist db ok', flush=True)
    except Exception as e:
        print('write back alist db failed: %s' % e, flush=True)


def write_back_seed(access_token, refresh_token):
    try:
        seed = read_json(SEED_FILE)
        if not seed:
            return
        seed['refresh_token'] = refresh_token
        if 'access_token' in seed:
            seed['access_token'] = access_token
        with open(SEED_FILE, 'w') as f:
            json.dump(seed, f, ensure_ascii=False)
        print('write back seed ok', flush=True)
    except Exception as e:
        print('write back seed failed: %s' % e, flush=True)


# ---------------- 刷新与验证 ----------------
def refresh(rt):
    return http_json(AUTH_URL, {'grant_type': 'refresh_token', 'refresh_token': rt})


def verify(access_token):
    """刷新后实测, 避免拿到『看起来成功但实际无效』的 token。"""
    if VERIFY_DRIVE_ID:
        d = http_json(LIST_URL, {'drive_id': VERIFY_DRIVE_ID, 'parent_file_id': 'root', 'limit': 1},
                      token=access_token)
    else:
        d = http_json(USER_URL, {}, token=access_token)
    if d.get('user_id') or d.get('name') or 'items' in d:
        return True, 'ok'
    return False, '%s %s' % (d.get('code', ''), str(d.get('message', ''))[:120])


def main():
    force_ipv4()

    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    lock = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('another refresh running, skip', flush=True)
        return 0

    cur = read_json(TOKEN_FILE)
    at, rt_cur = cur.get('access_token', ''), cur.get('refresh_token', '')
    exp = jwt_exp(at)
    if exp - int(time.time()) > 600:
        ok = True
        try:
            ok, msg = verify(at)
        except Exception as e:
            ok, msg = False, str(e)
        if ok:
            write_state(exp=exp, fail_count=0, last_ok=int(time.time()))
            print('token valid, exp in %ds' % (exp - int(time.time())), flush=True)
            return 0
        print('local token invalid by verify: %s' % msg, flush=True)

    cands = candidates()
    if not cands:
        write_state(fail_count=read_json(STATE_FILE).get('fail_count', 0) + 1,
                    last_fail=int(time.time()), last_error='no candidate')
        print('no refresh_token candidate found', flush=True)
        return 2

    for src, rt in cands:
        print('try candidate: %s' % src, flush=True)
        d = refresh(rt)
        new_at = d.get('access_token', '')
        if not new_at:
            print('  refresh failed: %s %s' % (d.get('code', ''), str(d.get('message', d.get('error', '')))[:160]), flush=True)
            continue
        new_rt = d.get('refresh_token') or rt
        ok, msg = verify(new_at)
        if not ok:
            print('  verify failed(%s), switch candidate' % msg, flush=True)
            continue

        # 成功: 先备份旧 token, 再落盘 + 回写 alist/seed 保持同代
        if at or rt_cur:
            try:
                with open(PREV_FILE, 'w') as f:
                    json.dump({'access_token': at, 'refresh_token': rt_cur,
                               'expires_in': cur.get('expires_in', 7200)}, f, ensure_ascii=False)
            except Exception as e:
                print('backup .prev failed: %s' % e, flush=True)
        payload = {'access_token': new_at, 'refresh_token': new_rt,
                   'expires_in': d.get('expires_in', 7200)}
        with open(TOKEN_FILE, 'w') as f:
            json.dump(payload, f, ensure_ascii=False)
        write_back_alist(new_at, new_rt)
        write_back_seed(new_at, new_rt)
        exp = jwt_exp(new_at) or int(time.time()) + int(d.get('expires_in', 7200))
        write_state(exp=exp, fail_count=0, last_ok=int(time.time()),
                    source=src, last_error='')
        print('token refreshed ok via %s, exp in %ds' % (src, exp - int(time.time())), flush=True)
        return 0

    fc = int(read_json(STATE_FILE).get('fail_count', 0)) + 1
    write_state(fail_count=fc, last_fail=int(time.time()), last_error='all candidates failed')
    print('all candidates failed (fail_count=%d)' % fc, flush=True)
    return 2


if __name__ == '__main__':
    sys.exit(main())
