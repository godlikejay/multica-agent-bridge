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
BROWSER = 'http://localhost:13000'
WEB = 'http://127.0.0.1:13000'
DOCKER_API = 'http://backend:8080'
MARKER = 'E2E_SYNTHETIC_ONLY'
ESCAPED_MARKER = MARKER.replace('_', r'\_')
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
RUN_UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

def get(path, token=None, workspace_id=None, require_complete=False):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if workspace_id:
        headers['X-Workspace-ID'] = workspace_id
    with OPENER.open(urllib.request.Request(API + path, headers=headers), timeout=5) as r:
        if require_complete and (r.headers.get('X-Comments-Truncated', '').lower() == 'true'
                                 or r.headers.get('X-Multica-Next-Before')
                                 or r.headers.get('X-Multica-Next-Before-Id')):
            raise ValueError('Incomplete comment evidence; use a fresh synthetic Issue')
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
                 or destination.startswith(BROWSER + '/'))
        if not (300 <= exc.code < 400 and local):
            raise

def print_urls(web, host_api, docker_api):
    print(f'Browser URL (setup and test Agent UI): {web}')
    print(f'Host API (fixture verification only):   {host_api}')
    print(f'Docker API (Bridge / daemon login):    {docker_api}')


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
    body = e.get('body', '')
    if MARKER not in body and ESCAPED_MARKER not in body:
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

def local_comments(run):
    for p in (MAIL / 'outbox').glob('*.json'):
        if p.is_symlink() or not p.is_file() or p.stat().st_size > 256 * 1024:
            continue
        e = json.loads(p.read_text())
        if e.get('kind') == 'comment' and all(e.get(k) == run[k] for k in ('issue_id', 'run_id', 'epoch')):
            yield e

def require(condition, message):
    if not condition:
        raise SystemExit('FOLLOW-UP FAIL: ' + message)

def follow_up(mode, workspace, filename_old, filename_new, filename_history, filename_trigger):
    # Test actual application lifecycle on the isolated stack only. The human
    # operates the UI; this command writes only two deterministic marked actions.
    old, new, history = map(read_event, (filename_old, filename_new, filename_history))
    observed = read_event(filename_trigger) if filename_trigger else None
    require(old['kind'] == new['kind'] == 'task', 'provide two initial task events')
    require(old['issue_id'] == new['issue_id'] and old['run_id'] != new['run_id']
            and new['epoch'] == old['epoch'] + 1, 'expected consecutive Runs of one Issue')
    require((mode == 'queue') == (observed is not None),
            '--observed-trigger is required for queue only')
    require(all(RUN_UUID.fullmatch(x) for x in (workspace, old['issue_id'], old['run_id'], new['run_id'])),
            'expected canonical synthetic workspace, Issue and Run UUIDs')
    for e in [history] + ([observed] if observed else []):
        require(e['kind'] == 'comment' and all(e[k] == old[k] for k in ('issue_id', 'run_id', 'epoch'))
                and RUN_UUID.fullmatch(e.get('source_id') or '') and not e.get('run_input'),
                'expected an ordinary, already-observed comment on the old Run')
    token = prompt_token()
    issue = old['issue_id']
    issue_path = '/api/issues/' + issue
    runs = get(issue_path + '/task-runs', token, workspace)
    require(isinstance(runs, list), 'unrecognized remote Run list')
    a = [r for r in runs if r.get('id') == old['run_id']]
    b = [r for r in runs if r.get('id') == new['run_id']]
    require(len(a) == len(b) == 1, 'both exact Run IDs must exist on the remote Issue')
    a, b = a[0], b[0]
    require(all(r.get('issue_id') == issue and r.get('workspace_id') == workspace for r in (a, b))
            and bool(a.get('agent_id')) and a['agent_id'] == b.get('agent_id'),
            'remote workspace, Issue or Agent binding mismatch')
    require(a.get('status') in ({'completed'} if mode == 'queue' else {'cancelled', 'canceled'})
            and b.get('status') == 'running', 'expected old terminal Run and replacement running')
    trigger = b.get('trigger_comment_id')
    require(isinstance(trigger, str) and bool(RUN_UUID.fullmatch(trigger)) and trigger != history['source_id'],
            'missing remote primary trigger, or ordinary history was used as trigger')
    delivered = b.get('delivered_comment_ids')
    require(isinstance(delivered, list) and all(isinstance(x, str) and RUN_UUID.fullmatch(x) for x in delivered)
            and len(delivered) == len(set(delivered)) and trigger in delivered,
            'new Run has no claim-time delivery receipt for its primary trigger')
    require(not observed or observed['source_id'] == trigger,
            'new remote trigger is not the queued comment observed on the old Run')
    coalesced = b.get('coalesced_comment_ids', [])
    require(isinstance(coalesced, list) and all(isinstance(x, str) and RUN_UUID.fullmatch(x) for x in coalesced)
            and set(delivered) - {trigger} <= set(coalesced),
            'nonprimary delivery lacks a coalesced role in the remote Run')
    expected_roles = {source: 'trigger' if source == trigger else 'coalesced' for source in delivered}
    require(history['source_id'] not in expected_roles, 'ordinary history was delivered as Run input')
    if mode == 'queue':
        require(len(delivered) >= 2, 'queue case requires at least two delivered inputs')
        seen_before = {e.get('source_id') for e in local_comments(old)
                       if not e.get('run_input') and any(m in e.get('body', '') for m in (MARKER, ESCAPED_MARKER))}
        require(set(delivered) <= seen_before, 'a queued input was not observed as ordinary history on the old Run')

    def comments():
        rows = get(issue_path + '/comments?recent=200', token, workspace, require_complete=True)
        require(isinstance(rows, list) and all(r.get('issue_id') == issue for r in rows),
                'remote comment evidence is not scoped to this Issue')
        return rows

    matches = [r.get('id') for r in comments() if r.get('id') in expected_roles and not r.get('deleted_at')
               and any(m in r.get('content', '') for m in (MARKER, ESCAPED_MARKER))]
    require(len(matches) == len(expected_roles) and set(matches) == set(expected_roles),
            'remote Run inputs are not the explicit synthetic comments')

    def published_inputs():
        roles = {}
        for e in local_comments(new):
            source, role = e.get('source_id'), e.get('run_input')
            require(source != history['source_id'], 'ordinary observed history was re-emitted')
            if not role:
                require(source not in expected_roles, 'a delivered source was re-emitted without its Run input role')
                continue
            require(source not in roles and source in expected_roles and role == expected_roles[source],
                    'unexpected, duplicate or mislabelled Run input')
            roles[source] = e
        return roles

    # Allow the first replacement poll to publish all inputs after its task event.
    for _ in range(20):
        matched = published_inputs()
        if set(matched) == set(expected_roles):
            break
        time.sleep(1)
    else:
        raise SystemExit('FOLLOW-UP FAIL: not all delivered inputs were published for the replacement Run')
    require(not observed or matched[trigger].get('id') != observed['id'],
            'primary trigger did not get a new Run-bound event ID')

    nonce = uuid.uuid4().hex
    stale_body = MARKER + ': stale follow-up must never post ' + nonce
    fresh_body = MARKER + ': verified replacement reply ' + nonce
    receipt(submit(observed or old, 'reply', stale_body), 'stale', old)
    require(not any(stale_body in r.get('content', '') for r in comments()), 'stale action posted remotely')
    receipt(submit(matched[trigger], 'reply', fresh_body), 'reply_posted', new)
    rows = comments()
    require(not any(stale_body in r.get('content', '') for r in rows), 'stale action posted remotely')
    posted = [r for r in rows if fresh_body in r.get('content', '') and not r.get('deleted_at')]
    require(len(posted) == 1 and posted[0].get('source_task_id') == new['run_id']
            and posted[0].get('parent_id') == trigger and posted[0].get('author_id') == b['agent_id']
            and posted[0].get('author_type') == 'agent',
            'replacement reply is missing, duplicated, or remotely misbound')
    require(set(published_inputs()) == set(expected_roles), 'Run inputs changed during verification')
    print('REMOTE + MAILBOX FOLLOW-UP PASS:', mode, issue, old['run_id'], new['run_id'], trigger,
          'delivered inputs:', len(delivered))
    print('UI menu selection and timing must be checked separately; replacement Run left active.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('health')
    sub.add_parser('urls')
    f = sub.add_parser('fixture'); f.add_argument('event'); f.add_argument('--complete', action='store_true')
    sm = sub.add_parser('smoke'); sm.add_argument('event')
    pl = sub.add_parser('parallel'); pl.add_argument('event_a'); pl.add_argument('event_b'); pl.add_argument('--finish-a', action='store_true')
    fo = sub.add_parser('follow-up'); fo.add_argument('mode', choices=('queue', 'restart'))
    fo.add_argument('workspace'); fo.add_argument('old_task'); fo.add_argument('new_task')
    fo.add_argument('old_history'); fo.add_argument('--observed-trigger')
    v = sub.add_parser('verify'); v.add_argument('run_id')
    args = parser.parse_args()
    try:
        if args.cmd == 'health': health()
        elif args.cmd == 'urls': print_urls(BROWSER, API, DOCKER_API)
        elif args.cmd == 'fixture': fixture(args.event, args.complete)
        elif args.cmd == 'smoke':
            event = read_event(args.event)
            fixture(args.event, True)
            verify(event['run_id'])
        elif args.cmd == 'parallel': parallel(args.event_a, args.event_b, args.finish_a)
        elif args.cmd == 'follow-up':
            follow_up(args.mode, args.workspace, args.old_task, args.new_task, args.old_history, args.observed_trigger)
        else: verify(args.run_id)
    except (ValueError, OSError, urllib.error.URLError) as exc:
        raise SystemExit(type(exc).__name__ + ': fixture failed (check local setup; no pass).') from None
