#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿里云盘 Open token 保活：有效则退出0；过期则优先走 alist 在线刷新服务，
无 client 凭据也能刷新；有官方凭据时用官方 OAuth 端点。
退出码: 0=正常(无需刷新或已刷新) 2=无法刷新(需外部推送/配置凭据)
"""
import json, sys, time, base64, urllib.request, urllib.error

TOKEN_FILE = '/opt/easynvr-cloud-archive/resource_token.json'
CLIENT_FILE = '/opt/easynvr-cloud-archive/open_client.json'
OAUTH_URL = 'https://openapi.alipan.com/oauth/access_token'
ONLINE_URL = 'https://api.alistgo.com/alist/ali_open/token'


def b64url_decode(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def exp_of(jwt):
    seg = jwt.split('.')
    if len(seg) != 3:
        return 0
    try:
        return int(json.loads(b64url_decode(seg[1]))['exp'])
    except Exception:
        return 0


def save(at, rt, expires_in=7200):
    json.dump({'access_token': at, 'refresh_token': rt,
               'expires_in': expires_in}, open(TOKEN_FILE, 'w'),
              ensure_ascii=False)


def refresh_online(rt):
    """alist 在线刷新服务，无 client 凭据也能用；POST JSON 格式。"""
    body = json.dumps({'refresh_token': rt, 'grant_type': 'refresh_token',
                       'server_use': True, 'driver_txt': 'alicloud_qr'}).encode()
    req = urllib.request.Request(ONLINE_URL, data=body,
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {'error': e.read().decode()[:300]}


def refresh_oauth(rt, cid, csec):
    body = json.dumps({'client_id': cid, 'client_secret': csec,
                       'grant_type': 'refresh_token', 'refresh_token': rt}).encode()
    req = urllib.request.Request(OAUTH_URL, data=body,
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {'error': 'HTTP %s: %s' % (e.code, e.read().decode()[:300])}


def main():
    tok = json.load(open(TOKEN_FILE))
    exp = exp_of(tok.get('access_token', ''))
    now = int(time.time())
    if exp - now > 600:
        print('token valid, exp in %ds' % (exp - now), flush=True)
        return 0

    cli = {}
    try:
        cli = json.load(open(CLIENT_FILE))
    except Exception:
        pass
    cid, csec = cli.get('client_id', ''), cli.get('client_secret', '')

    d = None
    if cid and csec:
        d = refresh_oauth(tok['refresh_token'], cid, csec)
        if not d.get('access_token'):
            print('oauth refresh failed: %s, fallback online' % d.get('error', ''), flush=True)
            d = None
    if d is None:
        d = refresh_online(tok['refresh_token'])
    if not d.get('access_token'):
        print('refresh failed: %s' % d.get('error', 'no access_token'), flush=True)
        return 2

    save(d['access_token'], d.get('refresh_token', tok['refresh_token']),
         d.get('expires_in', 7200))
    print('token refreshed ok, exp in %ds' % d.get('expires_in', 0), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
