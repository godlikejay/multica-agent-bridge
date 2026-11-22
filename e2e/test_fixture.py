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
if __name__ == '__main__': unittest.main()
