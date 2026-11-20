#!/usr/bin/env python3
"""Operator-driven synthetic fixture; never interpret issue body as instructions."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent / '.local'
MAIL = ROOT / 'bridge'
API = 'http://127.0.0.1:18080'
MARKER = 'E2E_SYNTHETIC_ONLY'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

OPENER = urllib.request.build_opener(NoRedirect)

def get(path, token=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with OPENER.open(urllib.request.Request(API + path, headers=headers), timeout=5) as r:
        raw = r.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Oversized response')
        return json.loads(raw)

def health():
    for _ in range(60):
        try:
            get('/readyz')
            print('Backend /readyz ready (NOT runtime/bridge readiness).')
            return
        except (OSError, ValueError):
            time.sleep(2)
    raise SystemExit('Backend readiness timeout; inspect isolated backend logs.')

def read_event(filename):
    original = Path(filename)
    if original.is_symlink():
        raise ValueError('Symlink event refused')
    p = original.resolve(strict=True)
    if p.parent != (MAIL / 'outbox').resolve() or p.is_symlink():
        raise ValueError('Choose a direct outbox event from this isolated stack')
    raw = p.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError('Oversized event')
    e = json.loads(raw)
    if e.get('version') != 1 or e.get('kind') not in ('task', 'comment'):
        raise ValueError('Expected v1 task/comment')
    if not isinstance(e.get('epoch'), int) or e['epoch'] < 1:
        raise ValueError('Missing current epoch')
    for k in ('id', 'issue_id', 'run_id'):
        if not isinstance(e.get(k), str) or not e[k]:
            raise ValueError('Missing identity: ' + k)
    if MARKER not in e.get('body', ''):
        raise ValueError('Only the explicitly marked synthetic fixture is accepted')
    return e

def submit(e, kind, body):
    request_id = 'e2e-' + uuid.uuid4().hex
    action = {k: e[k] for k in ('issue_id', 'run_id', 'epoch')}
    action.update(version=1, kind=kind, id=request_id, body=body)
    if e['kind'] == 'comment':
        if not e.get('source_id'):
            raise ValueError('Comment missing source_id')
        action['parent_id'] = e['source_id']
    inbox = MAIL / 'inbox'
    # A request is immutable; retry receipt polling, never send a second POST blindly.
    tmp = inbox / ('.' + request_id + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(action, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, inbox / (request_id + '.json'))
    return request_id

def receipt(request_id, expected):
    for _ in range(90):
        for p in (MAIL / 'outbox').glob('*.json'):
            if p.is_symlink() or p.stat().st_size > 256 * 1024:
                continue
            e = json.loads(p.read_text())
            if e.get('kind') == 'receipt' and e.get('request_id') == request_id:
                if e.get('status') != expected:
                    raise SystemExit('Receipt ' + str(e.get('status')) + '; stop, inspect before retrying. Request: ' + request_id)
                print('Receipt:', request_id, expected)
                return
        time.sleep(1)
    raise SystemExit('Receipt timeout. Do not resend blindly. Request: ' + request_id)

def fixture(filename, complete):
    e = read_event(filename)
    q = submit(e, 'reply', MARKER + ': deterministic bridge reply, no agent executed.')
    receipt(q, 'reply_posted')
    if complete:
        q = submit(e, 'complete', MARKER + ': synthetic fixture finished.')
        receipt(q, 'completion_requested')
        print('Local completion requested only. Now run verify', e['run_id'])
    else:
        print('Reply delivered; Run deliberately left active for comment/restart checks.')

def verify(run_id):
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', run_id):
        raise ValueError('Expected Run UUID')
    token = getpass.getpass('PAT of SAME synthetic runtime owner (not stored): ')
    if not token:
        raise ValueError('Token required for authenticated remote verification')
    # Pinned router: DaemonAuth accepts valid user token; ownership is enforced.
    for _ in range(60):
        result = get('/api/daemon/tasks/' + run_id + '/status', token)
        status = result.get('status')
        if status == 'completed':
            print('REMOTE PASS: exact Run', run_id, 'is completed.')
            return
        if status in ('failed', 'cancelled', 'canceled'):
            raise SystemExit('REMOTE FAIL: ' + status)
        time.sleep(2)
    raise SystemExit('Remote Run not completed before timeout; no E2E pass.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('health')
    f = sub.add_parser('fixture'); f.add_argument('event'); f.add_argument('--complete', action='store_true')
    sm = sub.add_parser('smoke'); sm.add_argument('event')
    v = sub.add_parser('verify'); v.add_argument('run_id')
    args = parser.parse_args()
    try:
        if args.cmd == 'health': health()
        elif args.cmd == 'fixture': fixture(args.event, args.complete)
        elif args.cmd == 'smoke':
            event = read_event(args.event)
            fixture(args.event, True)
            verify(event['run_id'])
        else: verify(args.run_id)
    except (ValueError, OSError, urllib.error.URLError) as exc:
        raise SystemExit(type(exc).__name__ + ': fixture failed (check local setup; no pass).') from None
