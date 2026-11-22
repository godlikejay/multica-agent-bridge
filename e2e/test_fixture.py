#!/usr/bin/env python3
"""Offline runner tests only; never connect to Multica or start containers."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('fixture', Path(__file__).with_name('fixture.py'))
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)

class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for d in ['outbox', 'inbox', 'state']: (self.root / d).mkdir()
        self.old = f.MAIL; f.MAIL = self.root
        self.event = dict(version=1, kind='task', id='event-1', issue_id='issue-1', run_id='run-1', epoch=2, body='E2E_SYNTHETIC_ONLY fixture')
        self.path = self.root / 'outbox/event-1.json'
        self.path.write_text(json.dumps(self.event))
    def tearDown(self):
        f.MAIL = self.old; self.tmp.cleanup()
    def test_atomic_submission_and_scope(self):
        e = f.read_event(self.path)
        rid = f.submit(e, 'reply', 'fixed fixture')
        inbox = self.root / 'inbox' / e['issue_id']
        output = json.loads((inbox / (rid + '.json')).read_text())
        self.assertEqual(output['issue_id'], e['issue_id'])
        self.assertEqual(output['run_id'], e['run_id'])
        self.assertEqual(output['epoch'], 2)
        self.assertEqual(list(inbox.glob('*.tmp')), [])
        self.assertEqual(list((self.root / 'inbox').glob('*.json')), [])
    def test_comment_parent(self):
        e = dict(self.event, kind='comment', source_id='comment-1')
        rid = f.submit(e, 'reply', 'fixed')
        self.assertEqual(json.loads((self.root/'inbox'/e['issue_id']/(rid+'.json')).read_text())['parent_id'], 'comment-1')
    def test_reject_unmarked_and_symlink(self):
        self.path.write_text(json.dumps(dict(self.event, body='Real task')))
        with self.assertRaises(ValueError): f.read_event(self.path)
        p = self.root/'outbox/link.json';p.symlink_to(self.path)
        with self.assertRaises(ValueError): f.read_event(p)
    def test_accept_markdown_escaped_marker_from_ui_editor(self):
        self.path.write_text(json.dumps(dict(self.event, kind='comment', source_id='comment-1', body=r'E2E\_SYNTHETIC\_ONLY: typed in UI editor')))
        e = f.read_event(self.path)
        self.assertEqual(e['source_id'], 'comment-1')
    def test_reject_bad_or_symlink_issue_folder(self):
        with self.assertRaises(ValueError): f.submit(dict(self.event, issue_id='../state'), 'reply', 'fixed')
        (self.root/'inbox/issue-1').symlink_to(self.root/'state', target_is_directory=True)
        with self.assertRaises(ValueError): f.submit(self.event, 'reply', 'fixed')
        self.assertEqual(list((self.root/'state').iterdir()), [])
    def test_uncertain_is_not_success(self):
        self.path.write_text(json.dumps(dict(kind='receipt', request_id='r1', status='delivery_uncertain')))
        with self.assertRaises(SystemExit): f.receipt('r1', 'reply_posted')
    def test_receipt_scope_must_match(self):
        bad = dict(self.event, kind='receipt', request_id='r1', status='reply_posted', issue_id='other')
        self.path.write_text(json.dumps(bad))
        with self.assertRaisesRegex(SystemExit, 'binding mismatch'): f.receipt('r1', 'reply_posted', self.event)
    def test_remote_status_required(self):
        with patch.object(f.getpass, 'getpass', return_value='synthetic'), patch.object(f, 'get', return_value={'status':'completed'}) as get:
            with contextlib.redirect_stdout(io.StringIO()): f.verify('00000000-0000-0000-0000-000000000001')
            self.assertIn('/api/daemon/tasks/', get.call_args.args[0])
        with patch.object(f.getpass, 'getpass', return_value='synthetic'), patch.object(f, 'get', return_value={'status':'failed'}):
            with self.assertRaises(SystemExit): f.verify('00000000-0000-0000-0000-000000000001')
    def test_display_fixed_browser_host_and_docker_addresses(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            f.print_urls(f.BROWSER, f.API, f.DOCKER_API)
        self.assertEqual([line.split()[-1] for line in out.getvalue().splitlines()], [
            'http://localhost:13000', 'http://127.0.0.1:18080', 'http://backend:8080',
        ])
    def test_health_checks_api_and_frontend_separately(self):
        with patch.object(f, 'get') as get, patch.object(f, 'web_ready') as web:
            with contextlib.redirect_stdout(io.StringIO()): f.health()
        get.assert_called_once_with('/readyz')
        web.assert_called_once_with()
    def test_frontend_accepts_only_local_redirects(self):
        for destination in ('/login', 'http://localhost:13000/login', 'https://example.invalid/', '//example.invalid/'):
            error = urllib.error.HTTPError(f.WEB, 302, 'redirect', {'Location': destination}, None)
            with patch.object(f.OPENER, 'open', side_effect=error):
                if 'example.invalid' in destination:
                    with self.assertRaises(urllib.error.HTTPError): f.web_ready()
                else:
                    f.web_ready()
    def two_tasks(self):
        a = dict(self.event, run_id='00000000-0000-0000-0000-000000000001')
        b = dict(self.event, id='event-2', issue_id='issue-2', run_id='00000000-0000-0000-0000-000000000002')
        self.path.write_text(json.dumps(a))
        second = self.root / 'outbox/event-2.json'
        second.write_text(json.dumps(b))
        return a, b, second
    def test_parallel_routes_actions_and_leaves_a_running(self):
        a, b, second = self.two_tasks()
        with patch.object(f.getpass, 'getpass', return_value='synthetic'), patch.object(f, 'receipt') as receipt, patch.object(f, 'get', side_effect=[{'status':s} for s in ('running', 'running', 'completed', 'running')]) as get:
            with contextlib.redirect_stdout(io.StringIO()): f.parallel(self.path, second, False)
        actions = [[json.loads(p.read_text()) for p in (self.root/'inbox'/e['issue_id']).glob('*.json')] for e in (a,b)]
        self.assertEqual([[(x['kind'], x['run_id']) for x in y] for y in actions], [[('reply',a['run_id'])],[('complete',b['run_id'])]])
        self.assertEqual([c.args[1:] for c in receipt.call_args_list], [('reply_posted',a),('completion_requested',b)])
        self.assertEqual([c.args[0].split('/')[-2] for c in get.call_args_list], [a['run_id'],b['run_id'],b['run_id'],a['run_id']])
    def test_parallel_refuses_same_issue_or_non_running(self):
        _, _, second = self.two_tasks()
        with self.assertRaisesRegex(ValueError, 'distinct Issues'): f.parallel(self.path, self.path, False)
        with patch.object(f.getpass, 'getpass', return_value='synthetic'), patch.object(f, 'get', side_effect=[{'status':'running'},{'status':'queued'}]):
            with self.assertRaisesRegex(SystemExit, 'Both Runs'): f.parallel(self.path, second, False)
        self.assertEqual(list((self.root/'inbox').iterdir()), [])
    def test_parallel_does_not_pass_if_a_stops(self):
        _, _, second = self.two_tasks()
        with patch.object(f.getpass, 'getpass', return_value='synthetic'), patch.object(f, 'receipt'), patch.object(f, 'get', side_effect=[{'status':s} for s in ('running', 'running', 'completed', 'failed')]):
            with self.assertRaisesRegex(SystemExit, 'A is no longer running'): f.parallel(self.path, second, True)
        self.assertEqual(len(list((self.root/'inbox/issue-1').glob('*.json'))), 1)
    def test_parallel_cleanup_is_explicit(self):
        _, _, second = self.two_tasks()
        with patch.object(f.getpass, 'getpass', return_value='synthetic'), patch.object(f, 'receipt'), patch.object(f, 'get', side_effect=[{'status':s} for s in ('running', 'running', 'completed', 'running', 'completed')]):
            with contextlib.redirect_stdout(io.StringIO()): f.parallel(self.path, second, True)
        kinds = [json.loads(p.read_text())['kind'] for p in (self.root/'inbox/issue-1').glob('*.json')]
        self.assertCountEqual(kinds, ['reply', 'complete'])

    def follow_up_fixture(self, mode, *, delivered=True, repeat_history=False, stale_posts=False,
                          first_seen=True, multiple=True, wrong_role=False, unexpected=False,
                          duplicate=False, missing=False):
        def uid(n):
            return '00000000-0000-0000-0000-' + str(n).zfill(12)

        workspace, issue, agent, trigger, history, first = map(uid, range(10, 16))
        a = dict(self.event, issue_id=issue, run_id=uid(1))
        b = dict(a, id='event-2', run_id=uid(2), epoch=a['epoch'] + 1)

        def put(e):
            path = self.root / 'outbox' / (e['id'] + '.json')
            path.write_text(json.dumps(e))
            return path

        task_a, task_b = put(a), put(b)
        hist_a = put(dict(a, kind='comment', id='history-a', source_id=history))
        trigger_a = put(dict(a, kind='comment', id='trigger-a', source_id=trigger)) if mode == 'queue' else None
        put(dict(b, kind='comment', id='trigger-b', source_id=trigger, run_input='trigger'))
        has_first = mode == 'queue' and multiple
        if has_first and first_seen:
            put(dict(a, kind='comment', id='first-a', source_id=first))
        if has_first and not missing:
            put(dict(b, kind='comment', id='first-b', source_id=first,
                     run_input='trigger' if wrong_role else 'coalesced'))
        if unexpected:
            put(dict(b, kind='comment', id='unexpected-b', source_id=uid(19), run_input='coalesced'))
        if duplicate:
            put(dict(b, kind='comment', id='duplicate-b', source_id=trigger, run_input='trigger'))
        if repeat_history:
            put(dict(b, kind='comment', id='history-b', source_id=history))
        runs = [dict(id=a['run_id'], issue_id=issue, workspace_id=workspace, agent_id=agent,
                     status='completed' if mode == 'queue' else 'cancelled'),
                dict(id=b['run_id'], issue_id=issue, workspace_id=workspace, agent_id=agent,
                     status='running', trigger_comment_id=trigger,
                     coalesced_comment_ids=[first] if has_first else [],
                     delivered_comment_ids=([first, trigger] if has_first else [trigger]) if delivered else [])]

        def fake_get(path, token, header_workspace, require_complete=False):
            self.assertEqual(token, 'synthetic')
            self.assertEqual(header_workspace, workspace)
            if path == '/api/issues/' + issue + '/task-runs':
                return runs
            self.assertEqual(path, '/api/issues/' + issue + '/comments?recent=200')
            self.assertTrue(require_complete)
            rows = [dict(id=trigger, issue_id=issue, content=f.MARKER + ': trigger')]
            if has_first:
                rows.append(dict(id=first, issue_id=issue, content=f.MARKER + ': first queued'))
            for req in (self.root / 'inbox' / issue).glob('*.json'):
                event = json.loads(req.read_text())
                if event['run_id'] == b['run_id'] or stale_posts:
                    rows.append(dict(issue_id=issue, content=event['body'], author_id=agent,
                                     author_type='agent', parent_id=event.get('parent_id'),
                                     source_task_id=event['run_id']))
            return rows

        return (mode, workspace, task_a, task_b, hist_a, trigger_a), fake_get, a, b

    def test_follow_up_verifies_both_routes_and_bound_receipts(self):
        for mode in ('queue', 'restart'):
            with self.subTest(mode=mode):
                args, remote, a, b = self.follow_up_fixture(mode)
                with patch.object(f, 'prompt_token', return_value='synthetic'), patch.object(f, 'get', side_effect=remote), patch.object(f, 'receipt') as receipt:
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        f.follow_up(*args)
                self.assertIn('REMOTE + MAILBOX FOLLOW-UP PASS: ' + mode, out.getvalue())
                self.assertEqual([c.args[1:] for c in receipt.call_args_list], [('stale', a), ('reply_posted', b)])
                for req in (self.root / 'inbox' / a['issue_id']).glob('*.json'):
                    req.unlink()
                for event in (self.root / 'outbox').glob('*.json'):
                    event.unlink()

    def test_follow_up_rejects_missing_delivery_and_repeated_history_before_write(self):
        for kwargs, expected in ((dict(delivered=False), 'claim-time delivery'),
                                 (dict(repeat_history=True), 'ordinary observed history'),
                                 (dict(multiple=False), 'at least two'),
                                 (dict(first_seen=False), 'not observed'),
                                 (dict(wrong_role=True), 'mislabelled'),
                                 (dict(unexpected=True), 'unexpected'),
                                 (dict(duplicate=True), 'duplicate'),
                                 (dict(missing=True), 'not all delivered inputs')):
            args, remote, _, _ = self.follow_up_fixture('queue', **kwargs)
            with patch.object(f, 'time') as timer, patch.object(f, 'prompt_token', return_value='synthetic'), patch.object(f, 'get', side_effect=remote):
                with self.assertRaisesRegex(SystemExit, expected):
                    f.follow_up(*args)
            self.assertEqual(list((self.root / 'inbox').iterdir()), [])
            if kwargs.get('missing'):
                self.assertEqual(timer.sleep.call_count, 20)
            for event in (self.root / 'outbox').glob('*.json'):
                event.unlink()

    def test_follow_up_fails_if_stale_request_posted_remotely(self):
        args, remote, a, _ = self.follow_up_fixture('queue', stale_posts=True)
        with patch.object(f, 'prompt_token', return_value='synthetic'), patch.object(f, 'get', side_effect=remote), patch.object(f, 'receipt') as receipt:
            with self.assertRaisesRegex(SystemExit, 'stale action posted remotely'):
                f.follow_up(*args)
        self.assertEqual([c.args[1:] for c in receipt.call_args_list], [('stale', a)])
if __name__ == '__main__': unittest.main()
