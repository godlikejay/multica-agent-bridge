#!/usr/bin/env python3
"""Offline runner tests only; never connect to Multica or start containers."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
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
        output = json.loads((self.root / 'inbox' / (rid + '.json')).read_text())
        self.assertEqual(output['issue_id'], e['issue_id'])
        self.assertEqual(output['run_id'], e['run_id'])
        self.assertEqual(output['epoch'], 2)
        self.assertEqual(list((self.root/'inbox').glob('*.tmp')), [])
    def test_comment_parent(self):
        e = dict(self.event, kind='comment', source_id='comment-1')
        rid = f.submit(e, 'reply', 'fixed')
        self.assertEqual(json.loads((self.root/'inbox'/(rid+'.json')).read_text())['parent_id'], 'comment-1')
    def test_reject_unmarked_and_symlink(self):
        self.path.write_text(json.dumps(dict(self.event, body='Real task')))
        with self.assertRaises(ValueError): f.read_event(self.path)
        p = self.root/'outbox/link.json';p.symlink_to(self.path)
        with self.assertRaises(ValueError): f.read_event(p)
    def test_uncertain_is_not_success(self):
        self.path.write_text(json.dumps(dict(kind='receipt', request_id='r1', status='delivery_uncertain')))
        with self.assertRaises(SystemExit): f.receipt('r1', 'reply_posted')
    def test_remote_status_required(self):
        with patch.object(f.getpass, 'getpass', return_value='synthetic'), patch.object(f, 'get', return_value={'status':'completed'}) as get:
            with contextlib.redirect_stdout(io.StringIO()): f.verify('00000000-0000-0000-0000-000000000001')
            self.assertIn('/api/daemon/tasks/', get.call_args.args[0])
        with patch.object(f.getpass, 'getpass', return_value='synthetic'), patch.object(f, 'get', return_value={'status':'failed'}):
            with self.assertRaises(SystemExit): f.verify('00000000-0000-0000-0000-000000000001')
if __name__ == '__main__': unittest.main()
