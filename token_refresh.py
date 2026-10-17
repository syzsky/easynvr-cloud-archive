#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿里云盘 Open token 保活：有效则退出0；过期且有官方凭据则刷新并写回。
退出码: 0=正常(无需刷新或已刷新) 2=无法刷新(需外部推送/配置凭据)
"""
import json, os, sys, time, base64, urllib.request, urllib.error

TOKEN_FILE = '/opt/easynvr-cloud-archive/resource_token.json'
CLIENT_FILE = '/opt/easynvr-cloud-archive/open_client.json'
REFRESH_URL = 'https://openapi.alipan.com/oauth/access_token'


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
    if not cid or not csec:
        print('token expired but no client credentials, need external push', flush=True)
        return 2
    body = json.dumps({
        'client_id': cid, 'client_secret': csec,
        'grant_type': 'refresh_token', 'refresh_token': tok['refresh_token']
    }).encode()
    req = urllib.request.Request(REFRESH_URL, data=body,
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print('refresh failed HTTP %s: %s' % (e.code, e.read().decode()[:200]), flush=True)
        return 2
    if not d.get('access_token'):
        print('refresh failed: no access_token in resp', flush=True)
        return 2
    json.dump({'access_token': d['access_token'],
               'refresh_token': d.get('refresh_token', tok['refresh_token']),
               'expires_in': d.get('expires_in', 7200)},
              open(TOKEN_FILE, 'w'), ensure_ascii=False)
    print('token refreshed ok, exp in %ds' % d.get('expires_in', 0), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
