#!/usr/bin/env python3
"""Seed a fresh, loopback-only Multica v0.11.1 E2E stack via its normal APIs."""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

from fixture import (API, ATTACHMENT_BYTES, ATTACHMENT_NAME, COMMENT_ATTACHMENT_BYTES,
                     COMMENT_ATTACHMENT_NAME, FOREIGN_ATTACHMENT_BYTES,
                     FOREIGN_ATTACHMENT_NAME, MARKER, OPENER, ROOT, UUID, WORKSPACE, expect_run_inputs,
                     receipt, require, smoke, submit, synthetic_workspace, token_for_file)

# These are the disposable fixture values in compose.yaml, not user settings.
EMAIL = 'bridge-e2e@example.invalid'
MEMBER_EMAIL = 'bridge-e2e-member@example.invalid'
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


def upload_attachment(filename, data, *, token, workspace, issue_id=None):
    boundary = 'mab_fixture_' + uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             'Content-Type: text/plain\r\n\r\n'.encode('ascii') + data + b'\r\n']
    if issue_id is not None:
        require(isinstance(issue_id, str) and UUID.fullmatch(issue_id), 'invalid fixture attachment Issue')
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="issue_id"\r\n\r\n'
                     f'{issue_id}\r\n'.encode('ascii'))
    parts.append(f'--{boundary}--\r\n'.encode('ascii'))
    req = urllib.request.Request(API + '/api/upload-file', method='POST', data=b''.join(parts), headers={
        'Authorization': 'Bearer ' + token, 'X-Workspace-ID': workspace, 'Accept': 'application/json',
        'Content-Type': 'multipart/form-data; boundary=' + boundary,
        'X-Client-Capabilities': 'stable_attachment_urls',
    })
    try:
        with OPENER.open(req, timeout=5) as response:
            raw = response.read(LIMIT + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise SystemExit(f'E2E FAIL: synthetic attachment upload: HTTP {code}; response hidden.') from None
    require(len(raw) <= LIMIT, 'oversized synthetic attachment upload response')
    row = json.loads(raw)
    aid = uuid_field(row, 'id')
    require(row.get('workspace_id') == workspace and row.get('filename') == filename
            and row.get('size_bytes') == len(data), 'synthetic upload metadata mismatch')
    # Request only the stable canonical download path, never a signed URL.
    return aid, '/api/attachments/' + aid + '/download'


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
    jwt = login(EMAIL)
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


def login(email):
    request('POST', '/auth/send-code', {'email': email})
    return auth_token(request('POST', '/auth/verify-code', {'email': email, 'code': '888888'}))


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


def dispatch(token, with_attachments=False):
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
    agent = request('POST', '/api/agents', {'name': AGENT, 'runtime_id': runt,
                                           'permission_mode': 'private'}, **scope)
    agent_id = uuid_field(agent, 'id')
    require(agent.get('workspace_id') == workspace and agent.get('runtime_id') == runt,
            'seed Agent workspace or runtime mismatch')

    issue_payload = {
        'title': MARKER + ': bridge protocol smoke',
        'description': MARKER + ': disposable protocol fixture with no real work.',
        'status': 'todo',
        'assignee_type': 'agent',
        'assignee_id': agent_id,
    }
    if with_attachments:
        attachment_id, url = upload_attachment(ATTACHMENT_NAME, ATTACHMENT_BYTES, **scope)
        issue_payload['attachment_ids'] = [attachment_id]
        issue_payload['description'] += '\n\n[' + ATTACHMENT_NAME + '](' + url + ')'
    issue = request('POST', '/api/issues', issue_payload, **scope)
    issue_id = uuid_field(issue, 'id')
    require(issue.get('workspace_id') == workspace and issue.get('assignee_type') == 'agent'
            and issue.get('assignee_id') == agent_id, 'seed Issue workspace or assignee mismatch')
    print('Issued one synthetic task:', issue_id)


def implicit_follow_up(owner, owner_id, member, member_id, agent_id, task, parent_id, after_claim=None):
    issue, original = task['issue_id'], task['run_id']
    path = '/api/issues/' + issue
    content = 'Synthetic thread reply without an explicit mention'

    def runs():
        return list_rows(request('GET', path + '/task-runs?scope=issue', **owner), 'Issue Run')

    def post(actor, actor_id):
        posted = request('POST', path + '/comments',
                         {'content': content, 'type': 'comment', 'parent_id': parent_id}, **actor)
        require(posted.get('issue_id') == issue and posted.get('parent_id') == parent_id
                and posted.get('author_type') == 'member' and posted.get('author_id') == actor_id
                and posted.get('content') == content and not posted.get('trigger_outcomes'),
                'implicit follow-up was not saved as a plain reply to the exact parent')
        return uuid_field(posted, 'id')

    def original_running(rows):
        return any(r.get('id') == original and r.get('status') == 'running'
                   and r.get('agent_id') == agent_id for r in rows)

    def carries(run, source_id):
        return (run.get('trigger_comment_id') == source_id
                or source_id in (run.get('coalesced_comment_ids') or [])
                or source_id in (run.get('delivered_comment_ids') or []))

    initial = runs()
    require(len(initial) == 1 and original_running(initial),
            'implicit follow-up needs the original fixture Run to remain active')
    denied_id = post(member, member_id)
    comments = list_rows(request('GET', path + '/comments', **owner), 'Issue comment')
    require(any(c.get('id') == denied_id and c.get('parent_id') == parent_id
                and c.get('author_id') == member_id and c.get('content') == content for c in comments),
            'member fixture reply not independently visible on the original thread')
    before_allow = runs()
    require(len(before_allow) == 1 and original_running(before_allow)
            and not any(carries(r, denied_id) for r in before_allow),
            'member without permission changed the running fixture’s queue or input')
    print('Thread reply denied: comment saved while original Run remained active:', denied_id)

    allowed_id = post(owner, owner_id)
    require(allowed_id != denied_id, 'distinct thread replies reused the same ID')
    queued = runs()
    require(original_running(queued) and len(queued) <= 2
            and not any(carries(r, denied_id) for r in queued),
            'start-after reply interrupted current Run or added a denied input')
    require(all(r.get('id') == original
                or (r.get('status') in ('pending', 'queued', 'deferred') and carries(r, allowed_id))
                for r in queued), 'unexpected pending Run before the original completion')
    # Some upstream queues persist a second row, others defer to completion's
    # reconciliation. In either case only the owner reply may be claimed next.
    print('Owner thread reply saved without stopping the original Run:', allowed_id)

    def after_complete():
        deadline = time.monotonic() + 60
        while True:
            rows = runs()
            require(len(rows) <= 2 and not any(carries(r, denied_id) for r in rows),
                    'denied thread reply entered an upstream Run')
            next_rows = [r for r in rows if r.get('id') != original]
            if next_rows:
                run = next_rows[0]
                require(run.get('issue_id') == issue and run.get('agent_id') == agent_id
                        and run.get('workspace_id') == owner['workspace']
                        and run.get('trigger_comment_id') == allowed_id,
                        'next Run does not match the exact authorized thread reply')
                require(run.get('status') not in ('failed', 'cancelled', 'canceled'),
                        'next Run failed before the queued thread reply was claimed')
                if run.get('status') == 'running' and run.get('delivered_comment_ids'):
                    require(run['delivered_comment_ids'] == [allowed_id],
                            'next Run did not deliver only the authorized thread reply')
                    run_id = uuid_field(run, 'id')
                    print('THREAD REPLY PROTOCOL E2E PASS: exact queued reply claimed:', run_id,
                          'trigger:', allowed_id)
                    if after_claim:
                        after_claim(issue, run_id, parent_id)
                    else:
                        request('POST', path + '/tasks/' + run_id + '/cancel', {}, **owner)
                    return
            require(time.monotonic() < deadline, 'authorized thread reply was not claimed after completion')
            time.sleep(1)

    return after_complete


def stop_and_restart(owner, agent, issue, active, parent):
    """Replay the UI's two requests without assuming when the daemon polls."""
    path = '/api/issues/' + issue

    def runs():
        return list_rows(request('GET', path + '/task-runs?scope=issue', **owner), 'Issue Run')

    def post(label, *, quiet=False):
        content = MARKER + ': restart ' + label
        payload = {'content': content, 'type': 'comment', 'parent_id': parent}
        if quiet:
            payload['suppress_agent_ids'] = [agent]
        row = request('POST', path + '/comments', payload, **owner)
        require(row.get('issue_id') == issue and row.get('parent_id') == parent
                and row.get('content') == content, 'restart fixture reply was not saved')
        return uuid_field(row, 'id')

    def cancel(run):
        row = request('POST', path + '/tasks/' + run + '/cancel', {}, **owner)
        require(row.get('id') == run and row.get('status') == 'cancelled',
                'Multica did not confirm cancellation of the exact requested Run')

    def belongs_to(row, comment):
        return (row.get('trigger_comment_id') == comment
                or comment in (row.get('coalesced_comment_ids') or []))

    before = runs()
    old_ids = {uuid_field(row, 'id') for row in before}
    require(any(row.get('id') == active and row.get('status') == 'running'
                and row.get('agent_id') == agent for row in before),
            'restart fixture needs an exact running Run')
    b = post('B / only comment', quiet=True)
    require({uuid_field(row, 'id') for row in runs()} == old_ids,
            'only-comment reply unexpectedly added a Run')
    c = post('C / after current Run')
    queued = runs()
    pending = [row for row in queued if row.get('id') not in old_ids]
    require(any(row.get('id') == active and row.get('status') == 'running' for row in queued)
            and len(pending) == 1 and pending[0].get('status') in ('pending', 'queued', 'deferred')
            and belongs_to(pending[0], c),
            'restart fixture did not establish a queued C behind running A')
    queued_id = uuid_field(pending[0], 'id')
    cancel(active)
    d = post('D / restart')
    require(len({b, c, d}) == 3, 'restart fixture comment IDs are not distinct')

    # C can be claimed between the cancellation request and the POST for D.
    # If so, finish C to let the real dispatcher expose D separately. Do not
    # guess ownership from comment timestamps or the text sent by the fixture.
    seen = set()
    deadline = time.monotonic() + 90
    while True:
        rows = runs()
        require(any(row.get('id') == active and row.get('status') == 'cancelled' for row in rows),
                'original Run is not cancelled upstream')
        for row in rows:
            run = uuid_field(row, 'id')
            delivered = row.get('delivered_comment_ids') or []
            require(b not in delivered, 'only-comment B was delivered as a Run input')
            if run in old_ids or run in seen or row.get('status') != 'running' or not delivered:
                continue
            require(row.get('issue_id') == issue and row.get('agent_id') == agent
                    and row.get('workspace_id') == owner['workspace']
                    and set(delivered) <= {c, d} and len(set(delivered)) == len(delivered),
                    'unexpected Run or inputs in upstream restart claim')
            event = expect_run_inputs(issue, run, delivered)
            seen.add(run)
            if d in delivered:
                c_state = next((r.get('status') for r in rows if r.get('id') == queued_id), 'not listed')
                print('STOP AND RESTART PROTOCOL E2E PASS: cancelled A:', active,
                      'queued C:', queued_id, 'C status:', c_state,
                      'D delivered by:', run, 'co-delivered C:', c in delivered,
                      'B (not an input):', b, 'C:', c, 'D:', d)
                cancel(run)  # Free the synthetic daemon for the independent permission check.
                return
            require(delivered == [c] and len(seen) == 1, 'unexpected claim before D')
            receipt(submit(event, 'complete', MARKER + ': finish queued C'), 'completion_requested', event)
        require(time.monotonic() < deadline, 'Multica did not dispatch restart D')
        time.sleep(1)


def invocation_protocol(owner_token, with_attachments=False):
    workspace = synthetic_workspace(owner_token)
    owner = {'token': owner_token, 'workspace': workspace}
    owner_id = uuid_field(request('GET', '/api/me', token=owner_token), 'id')
    member_token = login(MEMBER_EMAIL)
    member_id = uuid_field(request('GET', '/api/me', token=member_token), 'id')
    require(member_id != owner_id, 'permission test requires distinct members')
    member = {'token': member_token, 'workspace': workspace}
    invite = request('POST', '/api/workspaces/' + workspace + '/members',
                     {'email': MEMBER_EMAIL, 'role': 'member'}, **owner)
    accepted = request('POST', '/api/invitations/' + uuid_field(invite, 'id') + '/accept',
                       token=member_token)
    require(accepted.get('role') == 'member' and accepted.get('user_id') == member_id
            and accepted.get('workspace_id') == workspace, 'permission test membership mismatch')

    agents = list_rows(request('GET', '/api/agents?include_archived=true', **owner), 'agent')
    matches = [a for a in agents if a.get('name') == AGENT and a.get('workspace_id') == workspace
               and a.get('owner_id') == owner_id and a.get('permission_mode') == 'private']
    require(len(matches) == 1, 'permission test needs the owner’s private fixture Agent')
    agent_id = uuid_field(matches[0], 'id')
    other_attachment_url = comment_attachment = None
    if with_attachments:
        other = request('POST', '/api/issues', {'title': 'Disposable foreign attachment fixture',
                                               'status': 'todo'}, **owner)
        other_id = uuid_field(other, 'id')
        require(other.get('workspace_id') == workspace and not other.get('assignee_id'),
                'foreign attachment fixture must be an unassigned local Issue')
        _, other_attachment_url = upload_attachment(FOREIGN_ATTACHMENT_NAME, FOREIGN_ATTACHMENT_BYTES,
                                                    issue_id=other_id, **owner)
        comment_attachment = upload_attachment(COMMENT_ATTACHMENT_NAME, COMMENT_ATTACHMENT_BYTES, **owner)
    smoke(owner_token, follow_up=lambda task, parent: implicit_follow_up(
        owner, owner_id, member, member_id, agent_id, task, parent,
        after_claim=lambda issue, run, parent_id: stop_and_restart(owner, agent_id, issue, run, parent_id)),
          foreign_attachment_url=other_attachment_url, comment_attachment=comment_attachment)
    # Separate and unassigned: no automatic run/source and no existing mention thread.
    issue = request('POST', '/api/issues', {'title': 'Disposable invocation permission fixture',
                                           'status': 'todo'}, **owner)
    issue_id = uuid_field(issue, 'id')
    require(issue.get('workspace_id') == workspace and not issue.get('assignee_id'),
            'permission test Issue is not an unassigned fixture')
    base = '/api/issues/' + issue_id
    # Exact same explicit target and text; only the posting principal changes.
    content = f'[@Synthetic private agent](mention://agent/{agent_id}) synthetic invocation check'

    def post(actor, actor_id, outcome, reason):
        posted = request('POST', base + '/comments', {'content': content, 'type': 'comment'}, **actor)
        source = uuid_field(posted, 'id')
        require(posted.get('issue_id') == issue_id and posted.get('author_id') == actor_id
                and posted.get('author_type') == 'member' and posted.get('content') == content,
                'upstream comment posting proof did not match the expected actor or Issue')
        targets = list_rows(posted.get('trigger_outcomes'), 'trigger outcome')
        require(len(targets) == 1 and targets[0].get('target_type') == 'agent'
                and targets[0].get('target_id') == agent_id and targets[0].get('status') == outcome
                and targets[0].get('reason_code') == reason,
                'upstream did not return the expected invocation outcome: ' + outcome + '/' + reason)
        return source

    def runs():
        return list_rows(request('GET', base + '/task-runs?scope=issue', **owner), 'Issue Run')

    require(not runs(), 'unassigned permission test fixture already has a Run')
    denied_id = post(member, member_id, 'blocked', 'invocation_not_allowed')
    comments = list_rows(request('GET', base + '/comments', **owner), 'Issue comment')
    require(any(c.get('id') == denied_id and c.get('author_id') == member_id
                and c.get('content') == content for c in comments),
            'denied member’s comment was not independently readable on the Issue')
    require(not runs(), 'denied member unexpectedly queued or triggered a Run')
    print('Invocation denied: comment saved; private target explicitly blocked; no Run:', denied_id)

    allowed_id = post(owner, owner_id, 'queued', 'queued')
    require(denied_id != allowed_id, 'two distinct comments returned an identical ID')
    deadline = time.monotonic() + 60
    while True:
        rows = runs()
        require(len(rows) <= 1, 'permission test observed an unexpected additional Run')
        if rows:
            run = rows[0]
            require(run.get('issue_id') == issue_id and run.get('workspace_id') == workspace
                    and run.get('agent_id') == agent_id and run.get('trigger_comment_id') == allowed_id,
                    'upstream Run was not bound to the exact authorized mention')
            require(run.get('status') not in ('failed', 'cancelled', 'canceled'),
                    'authorized mention produced a failed or cancelled Run')
            delivered = run.get('delivered_comment_ids')
            if run.get('status') in ('running', 'completed') and delivered:
                require(delivered == [allowed_id], 'upstream delivered IDs included unauthorized or unexpected inputs')
                print('INVOCATION PROTOCOL E2E PASS: exact authorized Run claimed:',
                      uuid_field(run, 'id'), 'trigger:', allowed_id)
                return
        require(time.monotonic() < deadline, 'authorized mention did not produce an exact claimed Run')
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('auth', 'dispatch', 'invocation'))
    parser.add_argument('--token-file', type=Path, default=ROOT / 'pat')
    parser.add_argument('--with-attachments', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'auth':
            auth(args.token_file)
        else:
            (dispatch if args.command == 'dispatch' else invocation_protocol)(
                token_for_file(args.token_file), with_attachments=args.with_attachments)
    except (ValueError, OSError):
        raise SystemExit('E2E FAIL: private PAT file or local stack unavailable.') from None


if __name__ == '__main__':
    main()
