#!/usr/bin/env python3
"""Seed a fresh, loopback-only Multica v0.11.1 E2E stack via its normal APIs."""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from fixture import API, MARKER, OPENER, ROOT, UUID, WORKSPACE, require, synthetic_workspace, token_for_file

# These are the disposable fixture values in compose.yaml, not user settings.
EMAIL = 'bridge-e2e@example.invalid'
WORKSPACE_SLUG = 'bridge-e2e-only'
DEVICE = 'synthetic-bridge-e2e'
AGENT = 'Synthetic Bridge E2E'
LIMIT = 1024 * 1024


def request(method, path, body=None, *, token=None, workspace=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if workspace:
        headers['X-Workspace-ID'] = workspace
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=5) as response:
            raw = response.read(LIMIT + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise SystemExit(f'E2E FAIL: seed {method} {path}: HTTP {code}; response hidden.') from None
    except OSError:
        raise SystemExit(f'E2E FAIL: seed {method} {path}: local API unavailable.') from None
    require(len(raw) <= LIMIT, 'oversized response during seed ' + path)
    try:
        return json.loads(raw) if raw else None
    except (ValueError, UnicodeError):
        raise SystemExit('E2E FAIL: invalid JSON during seed ' + path) from None


def uuid_field(row, key):
    value = row.get(key) if isinstance(row, dict) else None
    require(isinstance(value, str) and UUID.fullmatch(value),
            'seed received an invalid ' + key)
    return value


def list_rows(value, kind):
    require(isinstance(value, list) and all(isinstance(row, dict) for row in value),
            'seed received an invalid ' + kind + ' list')
    return value


def auth(token_file):
    require(not os.path.lexists(token_file), 'refusing to replace an existing PAT file')
    request('POST', '/auth/send-code', {'email': EMAIL})
    session = request('POST', '/auth/verify-code', {'email': EMAIL, 'code': '888888'})
    jwt = auth_token(session)
    workspaces = list_rows(request('GET', '/api/workspaces', token=jwt), 'workspace')
    require(not workspaces, 'expected a fresh synthetic account without workspaces')
    workspace = request('POST', '/api/workspaces',
                        {'name': WORKSPACE, 'slug': WORKSPACE_SLUG}, token=jwt)
    uuid_field(workspace, 'id')
    require(workspace.get('name') == WORKSPACE and workspace.get('slug') == WORKSPACE_SLUG,
            'seed workspace identity mismatch')
    pat = auth_token(request('POST', '/api/tokens',
                             {'name': 'Disposable Bridge E2E', 'expires_in_days': 1}, token=jwt),
                     prefix='mul_')
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(pat + '\n')
    print('Synthetic workspace created; one-day PAT saved privately.')


def auth_token(row, prefix=''):
    token = row.get('token') if isinstance(row, dict) else None
    require(isinstance(token, str) and len(prefix) < len(token) <= 4096
            and token.startswith(prefix) and all(33 <= ord(c) <= 126 for c in token),
            'seed received an invalid authentication response')
    return token


def wait_for_runtime(token, workspace):
    deadline = time.monotonic() + 90
    while True:
        runtimes = list_rows(request('GET', '/api/runtimes?owner=me',
                                     token=token, workspace=workspace), 'runtime')
        matches = [row for row in runtimes if
                   row.get('workspace_id') == workspace
                   and row.get('provider') == 'hermes'
                   and row.get('runtime_mode') == 'local'
                   and row.get('status') == 'online'
                   and isinstance(row.get('name'), str)
                   and row['name'].endswith(' (' + DEVICE + ')')
                   and row.get('daemon_id')]
        require(len(matches) <= 1, 'more than one matching synthetic Hermes runtime')
        if matches:
            return uuid_field(matches[0], 'id')
        require(time.monotonic() < deadline, 'synthetic Hermes runtime did not come online')
        time.sleep(1)


def dispatch(token):
    try:
        workspace = synthetic_workspace(token)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise SystemExit(f'E2E FAIL: synthetic workspace lookup: HTTP {code}.') from None
    scope = {'token': token, 'workspace': workspace}
    runt = wait_for_runtime(**scope)
    agents = list_rows(request('GET', '/api/agents?include_archived=true', **scope), 'agent')
    require(not any(a.get('name') == AGENT for a in agents),
            'test Agent already exists; use a fresh stack rather than dispatching again')
    agent = request('POST', '/api/agents', {'name': AGENT, 'runtime_id': runt}, **scope)
    agent_id = uuid_field(agent, 'id')
    require(agent.get('workspace_id') == workspace and agent.get('runtime_id') == runt,
            'seed Agent workspace or runtime mismatch')

    issue = request('POST', '/api/issues', {
        'title': MARKER + ': bridge protocol smoke',
        'description': MARKER + ': disposable protocol fixture with no real work.',
        'status': 'todo',
        'assignee_type': 'agent',
        'assignee_id': agent_id,
    }, **scope)
    issue_id = uuid_field(issue, 'id')
    require(issue.get('workspace_id') == workspace and issue.get('assignee_type') == 'agent'
            and issue.get('assignee_id') == agent_id, 'seed Issue workspace or assignee mismatch')
    print('Issued one synthetic task:', issue_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('auth', 'dispatch'))
    parser.add_argument('--token-file', type=Path, default=ROOT / 'pat')
    args = parser.parse_args()
    try:
        if args.command == 'auth':
            auth(args.token_file)
        else:
            dispatch(token_for_file(args.token_file))
    except (ValueError, OSError):
        raise SystemExit('E2E FAIL: private PAT file or local stack unavailable.') from None


if __name__ == '__main__':
    main()
