#!/usr/bin/env python3
"""Small offline checks for the one-stack harness; never starts Docker."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('fixture', Path(__file__).with_name('fixture.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def uid(n):
    return '00000000-0000-0000-0000-' + str(n).zfill(12)


class SmokeFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.mail = self.local / 'bridge'
        for name in ('inbox', 'outbox'):
            (self.mail / name).mkdir(parents=True)
        for k, v in (('ROOT', self.local), ('MAIL', self.mail)):
            helper = patch.object(f, k, v)
            helper.start()
            self.addCleanup(helper.stop)
        self.event = dict(id='test-event', version=1, kind='task', epoch=1, issue_id=uid(1),
                          run_id=uid(2), body=f.MARKER + ': test')
        self.task = self.mail / 'outbox/test-event.json'
        self.task.write_text(json.dumps(self.event))
        self.sync_event = dict(self.event, kind='sync', id='test-sync', status='healthy', input_ids=[])
        self.sync = self.task.with_name('test-sync.json')
        self.sync.write_text(json.dumps(self.sync_event))

    def test_task_marker_and_uniqueness(self):
        self.assertEqual(f.task_event(), self.event)
        duplicate = self.task.with_name('other.json')
        duplicate.write_text(json.dumps(dict(self.event, run_id=uid(3))))
        with self.assertRaisesRegex(SystemExit, 'expected one marked task'):
            f.task_event()
        duplicate.unlink()
        self.task.write_text(json.dumps(dict(self.event, body='not explicitly synthetic')))
        with self.assertRaisesRegex(SystemExit, 'expected one marked task'):
            f.task_event()

    def test_task_waits_briefly_without_writing(self):
        with (patch.object(f, 'outbox_records', side_effect=[[], [self.event]]),
              patch.object(f.time, 'sleep') as sleep):
            self.assertEqual(f.task_event(wait=2), self.event)
        sleep.assert_called_once_with(1)

    def test_token_file_private_and_plain_regular(self):
        pat = self.local / 'pat'
        pat.write_text('synthetic-test\n')
        pat.chmod(0o600)
        self.assertEqual(f.token_for_file(), 'synthetic-test')
        pat.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private file'):
            f.token_for_file()
        link = self.local / 'pat-link'
        link.symlink_to(pat)
        with self.assertRaises(OSError):
            f.token_for_file(link)

    def test_bootstrap_passes_token_only_by_stdin(self):
        with (patch.object(f, 'synthetic_workspace', return_value=uid(3)),
              patch.object(f.subprocess, 'run') as run,
              contextlib.redirect_stdout(io.StringIO()) as out):
            f.bootstrap('synthetic-secret')
        calls = run.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].kwargs['input'], 'synthetic-secret\n')
        self.assertEqual(calls[0].kwargs['stdout'], f.subprocess.DEVNULL)
        self.assertEqual(calls[0].kwargs['stderr'], f.subprocess.DEVNULL)
        for call in calls:
            self.assertNotIn('synthetic-secret', ' '.join(call.args[0]))
        self.assertNotIn('synthetic-secret', out.getvalue())

    def remote(self, status='running', comment_run=None, final='completed', remove_reply=True):
        workspace, agent, comment = uid(3), uid(4), uid(5)
        captured = {}

        def get(path, token, route=None, complete=False):
            self.assertEqual(token, 'test-pat')
            if path == '/api/workspaces':
                return [dict(id=workspace, name=f.WORKSPACE)]
            if path.endswith('/task-runs'):
                self.assertEqual(route, workspace)
                return [dict(id=self.event['run_id'], issue_id=self.event['issue_id'],
                             agent_id=agent, workspace_id=workspace, status=status)]
            if path.endswith('/comments?recent=200'):
                self.assertEqual(route, workspace)
                self.assertTrue(complete)
                return [dict(id=comment, issue_id=self.event['issue_id'], content=captured['body'],
                             author_type='agent', author_id=agent,
                             source_task_id=comment_run or self.event['run_id'])]
            self.assertEqual(path, '/api/daemon/tasks/' + self.event['run_id'] + '/status')
            return dict(status=final)

        def receipt(rid, kind, binding):
            self.assertEqual(binding, self.event)
            raw = (self.mail / 'inbox' / self.event['issue_id'] / (rid + '.json')).read_text()
            message = json.loads(raw)
            self.assertEqual([message[k] for k in f.BINDING], [self.event[k] for k in f.BINDING])
            self.assertEqual(message['kind'], 'reply' if kind == 'reply_posted' else 'complete')
            if kind == 'reply_posted':
                captured['body'] = message['body']
                if remove_reply and comment_run is None:
                    (self.mail / 'inbox' / self.event['issue_id'] / (rid + '.json')).unlink()
            return dict(remote_id=comment)

        return get, receipt

    def run_smoke(self, **kwargs):
        get, receipt = self.remote(**kwargs)
        with (patch.object(f, 'get', side_effect=get), patch.object(f, 'receipt', side_effect=receipt),
              contextlib.redirect_stdout(io.StringIO()) as out):
            f.smoke('test-pat')
        return out.getvalue()

    def test_smoke_verified_reply_then_exact_completion(self):
        out = self.run_smoke()
        self.assertIn('Initial sync verified with no Run comment inputs:', out)
        self.assertIn('Ordinary reply verified remotely:', out)
        self.assertIn('Confirmed reply removed from Inbox:', out)
        self.assertIn('REAL MULTICA E2E PASS: exact Run completed: ' + self.event['run_id'], out)

    def test_smoke_refuses_unbound_sync_before_write(self):
        self.sync.write_text(json.dumps(dict(self.sync_event, run_id=uid(6))))
        with (patch.object(f.time, 'sleep'),
              self.assertRaisesRegex(SystemExit, 'successful initial sync missing for the exact Run')):
            self.run_smoke()
        self.assertEqual(list((self.mail / 'inbox').iterdir()), [])

    def test_smoke_requires_explicit_empty_initial_run_input_ids(self):
        incomplete = dict(self.sync_event)
        del incomplete['input_ids']
        for event in (incomplete, dict(self.sync_event, input_ids=['unexpected-input'])):
            with self.subTest(event=event):
                self.sync.write_text(json.dumps(event))
                with self.assertRaisesRegex(SystemExit, 'expected explicit empty input_ids'):
                    self.run_smoke()
                self.assertEqual(list((self.mail / 'inbox').iterdir()), [])

    def test_smoke_expected_binding_before_write(self):
        with self.assertRaisesRegex(SystemExit, 'expected exact scoped Run'):
            self.run_smoke(status='queued')
        self.assertEqual(list((self.mail/'inbox').iterdir()), [])

    def test_smoke_does_not_complete_after_remote_reply_mismatch(self):
        with self.assertRaisesRegex(SystemExit, 'ordinary reply missing, duplicated, or not posted'):
            self.run_smoke(comment_run=uid(6))
        self.assertEqual(len(list((self.mail/'inbox'/self.event['issue_id']).glob('*.json'))), 1)

    def test_smoke_requires_confirmed_reply_cleanup(self):
        with (patch.object(f.time, 'sleep'),
              self.assertRaisesRegex(SystemExit, 'confirmed reply still present in Inbox')):
            self.run_smoke(remove_reply=False)
        self.assertEqual(len(list((self.mail/'inbox'/self.event['issue_id']).glob('*.json'))), 1)

    def test_smoke_requires_remote_completion_not_just_receipt(self):
        with self.assertRaisesRegex(SystemExit, 'remote Run failed'):
            self.run_smoke(final='failed')


if __name__ == '__main__':
    unittest.main()
