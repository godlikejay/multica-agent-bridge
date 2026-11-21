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
WEB = 'http://127.0.0.1:13000'
MARKER = 'E2E_SYNTHETIC_ONLY'
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
RUN_UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')

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

def wait_ready(name, check):
    for _ in range(60):
        try:
            check()
            print(name + ' ready (NOT runtime/bridge readiness).')
            return
        except (OSError, ValueError):
            time.sleep(2)
    raise SystemExit(name + ' readiness timeout; inspect isolated Compose logs.')

def web_ready():
    try:
        with OPENER.open(WEB + '/', timeout=5) as response:
            if response.status != 200:
                raise ValueError('Expected an HTML page or a local redirect')
    except urllib.error.HTTPError as exc:
        # A redirect to the frontend sign-in page is normal before bootstrap.
        destination = exc.headers.get('Location', '')
        exc.close()
        local = (destination.startswith('/') and not destination.startswith('//')
                 or destination.startswith(WEB + '/')
                 or destination.startswith('http://localhost:13000/'))
        if not (300 <= exc.code < 400 and local):
            raise

def health():
    wait_ready('Backend /readyz', lambda: get('/readyz'))
    wait_ready('Frontend', web_ready)

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
    if type(e.get('epoch')) is not int or e['epoch'] < 1:
        raise ValueError('Missing current epoch')
    for k in ('id', 'issue_id', 'run_id'):
        if not isinstance(e.get(k), str) or not ID.fullmatch(e[k]):
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
    if not ID.fullmatch(e['issue_id']):
        raise ValueError('Invalid Issue ID')
    inbox = MAIL / 'inbox' / e['issue_id']
    if inbox.parent.is_symlink():
        raise ValueError('Symlink inbox refused')
    inbox.mkdir(mode=0o700, exist_ok=True)
    if inbox.is_symlink() or not inbox.is_dir():
        raise ValueError('Only a real issue inbox directory is accepted')
    # A request is immutable; retry receipt polling, never send a second POST blindly.
    tmp = inbox / ('.' + request_id + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(action, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, inbox / (request_id + '.json'))
    return request_id

def receipt(request_id, expected, binding=None):
    for _ in range(90):
        for p in (MAIL / 'outbox').glob('*.json'):
            if p.is_symlink() or p.stat().st_size > 256 * 1024:
                continue
            e = json.loads(p.read_text())
            if e.get('kind') == 'receipt' and e.get('request_id') == request_id:
                if binding and any(e.get(k) != binding[k] for k in ('issue_id', 'run_id', 'epoch')):
                    raise SystemExit('Receipt binding mismatch. Request: ' + request_id)
                if e.get('status') != expected:
                    raise SystemExit('Receipt ' + str(e.get('status')) + '; stop, inspect before retrying. Request: ' + request_id)
                print('Receipt:', request_id, expected)
                return
        time.sleep(1)
    raise SystemExit('Receipt timeout. Do not resend blindly. Request: ' + request_id)

def fixture(filename, complete):
    e = read_event(filename)
    q = submit(e, 'reply', MARKER + ': deterministic bridge reply, no agent executed.')
    receipt(q, 'reply_posted', e)
    if complete:
        q = submit(e, 'complete', MARKER + ': synthetic fixture finished.')
        receipt(q, 'completion_requested', e)
        print('Local completion requested only. Now run verify', e['run_id'])
    else:
        print('Reply delivered; Run deliberately left active for comment/restart checks.')

def prompt_token():
    token = getpass.getpass('PAT of SAME synthetic runtime owner (not stored): ')
    if not token:
        raise ValueError('Token required for authenticated remote verification')
    return token

def run_status(run_id, token):
    if not RUN_UUID.fullmatch(run_id):
        raise ValueError('Expected Run UUID')
    # Pinned router: DaemonAuth accepts valid user token; ownership is enforced.
    return get('/api/daemon/tasks/' + run_id + '/status', token).get('status')

def wait_completed(run_id, token):
    for _ in range(60):
        status = run_status(run_id, token)
        if status == 'completed':
            return
        if status in ('failed', 'cancelled', 'canceled'):
            raise SystemExit('REMOTE FAIL: ' + status)
        time.sleep(2)
    raise SystemExit('Remote Run not completed before timeout; no E2E pass.')

def verify(run_id):
    wait_completed(run_id, prompt_token())
    print('REMOTE PASS: exact Run', run_id, 'is completed.')

def parallel(filename_a, filename_b, finish_a):
    a, b = read_event(filename_a), read_event(filename_b)
    if a['kind'] != 'task' or b['kind'] != 'task':
        raise ValueError('Choose two initial task events, not comments')
    if a['issue_id'] == b['issue_id'] or a['run_id'] == b['run_id']:
        raise ValueError('Choose distinct Issues and Runs')
    if not RUN_UUID.fullmatch(a['run_id']) or not RUN_UUID.fullmatch(b['run_id']):
        raise ValueError('Expected two Run UUIDs')
    token = prompt_token()
    for run in (a, b):
        if run_status(run['run_id'], token) != 'running':
            raise SystemExit('Both Runs must already be running; no inbox requests written.')
    receipt(submit(a, 'reply', MARKER + ': parallel reply to A only.'), 'reply_posted', a)
    receipt(submit(b, 'complete', MARKER + ': parallel completion of B only.'), 'completion_requested', b)
    wait_completed(b['run_id'], token)
    if run_status(a['run_id'], token) != 'running':
        raise SystemExit('REMOTE FAIL: A is no longer running after B completed.')
    if finish_a:
        receipt(submit(a, 'complete', MARKER + ': requested cleanup of A.'), 'completion_requested', a)
        wait_completed(a['run_id'], token)
        print('Requested cleanup: A is now completed.')
    print('REMOTE PARALLEL PASS: replied on A, B completed, and A stayed running after B.')
    print('Check both comments appear only on their intended Issues in the UI.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('health')
    f = sub.add_parser('fixture'); f.add_argument('event'); f.add_argument('--complete', action='store_true')
    sm = sub.add_parser('smoke'); sm.add_argument('event')
    pl = sub.add_parser('parallel'); pl.add_argument('event_a'); pl.add_argument('event_b'); pl.add_argument('--finish-a', action='store_true')
    v = sub.add_parser('verify'); v.add_argument('run_id')
    args = parser.parse_args()
    try:
        if args.cmd == 'health': health()
        elif args.cmd == 'fixture': fixture(args.event, args.complete)
        elif args.cmd == 'smoke':
            event = read_event(args.event)
            fixture(args.event, True)
            verify(event['run_id'])
        elif args.cmd == 'parallel': parallel(args.event_a, args.event_b, args.finish_a)
        else: verify(args.run_id)
    except (ValueError, OSError, urllib.error.URLError) as exc:
        raise SystemExit(type(exc).__name__ + ': fixture failed (check local setup; no pass).') from None
