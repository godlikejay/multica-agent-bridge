#!/usr/bin/env python3
"""One synthetic, loopback-only Multica acceptance check."""
import argparse
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import warnings

ROOT = Path(__file__).resolve().parent / '.local'
MAIL = ROOT / 'bridge'
API = 'http://127.0.0.1:18080'
BROWSER = 'http://localhost:13000'
WEB = 'http://127.0.0.1:13000'
DOCKER_API = 'http://backend:8080'
WORKSPACE = 'BRIDGE_E2E_ONLY'
MARKER = 'E2E_SYNTHETIC_ONLY'
ESCAPED_MARKER = MARKER.replace('_', r'\_')
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
BINDING = ('issue_id', 'run_id', 'epoch')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def require(condition, message):
    if not condition:
        raise SystemExit('E2E FAIL: ' + message)


def get(path, token=None, workspace=None, complete=False):
    return api_request(path, token, workspace, complete=complete)


def api_request(path, token, workspace, *, complete=False, payload=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if workspace:
        headers['X-Workspace-ID'] = workspace
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode()
    with OPENER.open(urllib.request.Request(API + path, headers=headers, data=data), timeout=5) as response:
        if complete and (response.headers.get('X-Comments-Truncated', '').lower() == 'true'
                         or response.headers.get('X-Multica-Next-Before')
                         or response.headers.get('X-Multica-Next-Before-Id')):
            raise ValueError('Incomplete remote comment snapshot; use a fresh synthetic Issue')
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Oversized response')
        return json.loads(raw)


def health():
    for name, check in (('Backend', lambda: get('/readyz')),
                        ('Frontend', lambda: OPENER.open(WEB + '/', timeout=5).close())):
        for _ in range(60):
            try:
                check()
            except urllib.error.HTTPError as exc:
                destination = exc.headers.get('Location', '')
                exc.close()
                if name != 'Frontend' or not (300 <= exc.code < 400 and (
                    destination.startswith('/') and not destination.startswith('//')
                    or destination.startswith(WEB + '/') or destination.startswith(BROWSER + '/')
                )):
                    time.sleep(1)
                    continue
            except (OSError, ValueError):
                time.sleep(1)
                continue
            print(name + ' ready; runtime readiness not yet checked.')
            break
        else:
            raise SystemExit(name + ' readiness timed out.')


def token_for_file(filename=None):
    # An explicit path must work; the default file may be absent for TTY use.
    path = Path(filename) if filename else ROOT / 'pat'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if filename:
            raise
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            token = getpass.getpass('Synthetic Multica PAT (not printed or stored): ').strip()
    else:
        with os.fdopen(fd, 'rb') as source:
            st = os.fstat(source.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise ValueError('PAT file must be a regular, user-owned private file (chmod 600)')
            raw = source.read(4097)
            if len(raw) > 4096:
                raise ValueError('PAT file is too large')
            token = raw.decode('utf-8').strip()
    if not token or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in token):
        raise ValueError('Expected one synthetic PAT')
    return token


def synthetic_workspace(token):
    workspaces = get('/api/workspaces', token)
    matches = [w.get('id') for w in workspaces if w.get('name') == WORKSPACE]
    require(len(matches) == 1 and isinstance(matches[0], str) and UUID.fullmatch(matches[0]),
            'expected exactly one workspace named ' + WORKSPACE + ' in the local test server')
    return matches[0]


def bootstrap(token):
    workspace = synthetic_workspace(token)
    base = ['docker', 'compose', '--env-file', str(ROOT / 'compose.env'), '-f',
            str(ROOT.parent / 'compose.yaml'), '--profile', 'runtime']
    runtime = base + ['run', '--rm', '-T', '--no-deps', 'runtime', '--profile', 'e2e']
    # Some CLI prompts echo tokens; discard both streams for login.
    try:
        subprocess.run(runtime + ['--server-url', DOCKER_API, 'login', '--token'],
                       input=token + '\n', text=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True, timeout=90)
    except (subprocess.SubprocessError, OSError):
        raise SystemExit('E2E bootstrap failed: synthetic CLI login did not complete.') from None
    subprocess.run(runtime + ['config', 'set', 'workspace_id', workspace], check=True, timeout=60)
    subprocess.run(base + ['up', '-d', 'runtime'], check=True, timeout=90)
    print('Daemon started for synthetic workspace', workspace)
    print('Verify synthetic-bridge-e2e is online in', BROWSER)


def outbox_records():
    for path in (MAIL / 'outbox').glob('*.json'):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            continue
        yield json.loads(path.read_text())


def task_event(wait=0):
    deadline = time.monotonic() + wait
    while True:
        tasks = [e for e in outbox_records() if e.get('kind') == 'task' and
                 isinstance(e.get('body'), str) and
                 any(marker in e['body'] for marker in (MARKER, ESCAPED_MARKER))]
        if tasks or time.monotonic() >= deadline:
            break
        time.sleep(1)
    require(len(tasks) == 1, 'expected one marked task event; check assignment or use a fresh local smoke fixture')
    e = tasks[0]
    require(e.get('version') == 1 and isinstance(e.get('id'), str) and ID.fullmatch(e['id'])
            and all(isinstance(e.get(k), str) and UUID.fullmatch(e[k]) for k in ('issue_id', 'run_id'))
            and type(e.get('epoch')) is int and e['epoch'] >= 1,
            'marked local task has invalid identity')
    return e


def submit(e, kind, body):
    request_id = 'e2e-' + uuid.uuid4().hex
    action = {k: e[k] for k in BINDING}
    action.update(version=1, kind=kind, id=request_id, body=body)
    inbox = MAIL / 'inbox' / e['issue_id']
    require(not inbox.parent.is_symlink(), 'symlink Inbox refused')
    inbox.mkdir(mode=0o700, exist_ok=True)
    require(not inbox.is_symlink() and inbox.is_dir(), 'Issue Inbox must be a real directory')
    tmp = inbox / ('.' + request_id + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(action, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(tmp, inbox / (request_id + '.json'))
    return request_id


def receipt(request_id, expected, binding):
    for _ in range(90):
        for e in outbox_records():
            if e.get('kind') == 'receipt' and e.get('request_id') == request_id:
                require(all(e.get(k) == binding[k] for k in BINDING), 'receipt binding mismatch')
                require(e.get('status') == expected,
                        'unexpected receipt; inspect before retrying request ' + request_id)
                print('Mailbox receipt:', request_id, expected)
                return e
        time.sleep(1)
    raise SystemExit('E2E FAIL: receipt timeout; do not resend. Request: ' + request_id)


def expect_initial_sync(binding):
    for _ in range(30):
        for e in outbox_records():
            if (e.get('kind') != 'sync' or e.get('status') not in ('healthy', 'recovered')
                    or not all(e.get(k) == binding[k] for k in BINDING)):
                continue
            require(e.get('input_ids') == [],
                    'expected explicit empty input_ids for the new synthetic Run without comments')
            print('Initial sync verified with no Run comment inputs:', binding['run_id'])
            return
        time.sleep(1)
    raise SystemExit('E2E FAIL: successful initial sync missing for the exact Run.')


def post_note(issue_path, token, workspace, body):
    result = api_request(issue_path + '/comments', token, workspace,
                         payload={'content': body, 'type': 'comment'})
    source_id = result.get('id') if isinstance(result, dict) else None
    require(isinstance(source_id, str) and UUID.fullmatch(source_id),
            'test note did not return a valid source ID')
    return source_id


def expect_issue_note(issue, source_id, body, discussion):
    # A note sent just after initial sync must survive the full default 30s
    # Bridge poll interval plus time for the next HTTP read and disk publish.
    for _ in range(45):
        events = [e for e in outbox_records()
                  if e.get('kind') == 'comment' and e.get('issue_id') == issue
                  and e.get('source_id') == source_id]
        if events:
            require(len(events) == 1, 'duplicate events for synthetic human note')
            e = events[0]
            require(e.get('run_id') == 'issue-context' and not e.get('run_input')
                    and e.get('body') == body and e.get('discussion') == discussion,
                    'synthetic human note was not classified as expected')
            try:
                created_at = datetime.fromisoformat(e.get('created_at', '').replace('Z', '+00:00'))
                require(created_at.utcoffset() is not None, 'note created_at has no timezone')
            except (AttributeError, ValueError):
                require(False, 'synthetic human note has no parseable created_at')
            return
        time.sleep(1)
    raise SystemExit('E2E FAIL: synthetic human note did not reach Issue context.')


def expect_inbox_cleaned(issue_id, request_id):
    path = MAIL / 'inbox' / issue_id / (request_id + '.json')
    for _ in range(20):
        if not os.path.lexists(path):
            print('Confirmed reply removed from Inbox:', request_id)
            return
        time.sleep(0.5)
    raise SystemExit('E2E FAIL: confirmed reply still present in Inbox: ' + request_id)


def smoke(token):
    workspace = synthetic_workspace(token)
    e = task_event(wait=60)
    issue, run = e['issue_id'], e['run_id']
    path = '/api/issues/' + issue
    runs = get(path + '/task-runs', token, workspace)
    matches = [r for r in runs if r.get('id') == run]
    require(len(matches) == 1, 'exact Run was not found on the remote Issue')
    row = matches[0]
    require(row.get('issue_id') == issue and row.get('workspace_id') == workspace
            and row.get('agent_id') and row.get('status') == 'running',
            'expected exact scoped Run to be running; no Inbox action written')
    print('Task claimed and read:', issue, run, 'epoch', e['epoch'])
    expect_initial_sync(e)

    notes = []
    for prefix, discussion in (('/note [mab-context] ', 'ordinary'), ('/note ', None)):
        note = prefix + MARKER + ': ' + uuid.uuid4().hex
        notes.append((post_note(path, token, workspace, note), note, discussion))
    # Let the worker pick up both notes in its next poll.
    for note_id, note, discussion in notes:
        expect_issue_note(issue, note_id, note, discussion)
    current_runs = get(path + '/task-runs', token, workspace)
    require({r.get('id') for r in current_runs} == {r.get('id') for r in runs},
            'synthetic human note unexpectedly created a Run')
    print('Issue context verified for marked and unmarked human notes without creating a Run:', issue)

    body = MARKER + ': ordinary smoke reply ' + uuid.uuid4().hex
    request_id = submit(e, 'reply', body)
    posted = receipt(request_id, 'reply_posted', e)
    comments = get(path + '/comments?recent=200', token, workspace, complete=True)
    delivered = [c for c in comments if body in c.get('content', '') and not c.get('deleted_at')]
    require(len(delivered) == 1 and posted.get('remote_id') and delivered[0].get('id') == posted['remote_id']
            and delivered[0].get('issue_id') == issue and delivered[0].get('source_task_id') == run
            and delivered[0].get('author_id') == row['agent_id'] and delivered[0].get('author_type') == 'agent',
            'ordinary reply missing, duplicated, or not posted by the exact Run on the exact Issue')
    print('Ordinary reply verified remotely:', posted['remote_id'])
    expect_inbox_cleaned(issue, request_id)

    receipt(submit(e, 'complete', MARKER + ': smoke complete.'), 'completion_requested', e)
    for _ in range(60):
        status = get('/api/daemon/tasks/' + run + '/status', token).get('status')
        if status == 'completed':
            print('REAL MULTICA E2E PASS: exact Run completed:', run)
            return
        require(status not in ('failed', 'cancelled', 'canceled'), 'remote Run failed or was cancelled')
        time.sleep(2)
    raise SystemExit('E2E FAIL: exact remote Run did not complete before timeout.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('health', 'bootstrap', 'smoke'))
    parser.add_argument('--token-file')
    args = parser.parse_args()
    try:
        if args.command == 'health':
            health()
        else:
            token = token_for_file(args.token_file)
            (bootstrap if args.command == 'bootstrap' else smoke)(token)
    except (ValueError, OSError, getpass.GetPassWarning, subprocess.SubprocessError):
        raise SystemExit('E2E FAIL: input or local stack unavailable; no pass. '
                         'Check the private PAT file, synthetic setup and Compose logs.') from None
