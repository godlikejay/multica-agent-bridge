"""Offline contract checks for seeding the disposable E2E stack."""
import contextlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import seed

WS = '00000000-0000-0000-0000-000000000001'
RUNTIME = '00000000-0000-0000-0000-000000000002'
AGENT = '00000000-0000-0000-0000-000000000003'
ISSUE = '00000000-0000-0000-0000-000000000004'
SYNTHETIC_PAT = 'mul_synthetic-fixture'
ONLINE = dict(id=RUNTIME, workspace_id=WS, daemon_id='synthetic-daemon',
              provider='hermes', runtime_mode='local', status='online',
              name='Hermes (' + seed.DEVICE + ')')


class SeedFixture(unittest.TestCase):
    def setUp(self):
        testdir = tempfile.TemporaryDirectory()
        self.addCleanup(testdir.cleanup)
        self.pat = Path(testdir.name) / 'pat'

    def test_disposable_account_issues_private_one_day_pat(self):
        with (patch.object(seed, 'request', side_effect=[
            {}, {'token': 'synthetic-jwt'}, [],
            {'id': WS, 'name': seed.WORKSPACE, 'slug': seed.WORKSPACE_SLUG},
            {'token': SYNTHETIC_PAT},
        ]) as request, contextlib.redirect_stdout(io.StringIO()) as logs):
            seed.auth(self.pat)
        calls = request.call_args_list
        self.assertEqual([c.args[:2] for c in calls], [
            ('POST', '/auth/send-code'), ('POST', '/auth/verify-code'),
            ('GET', '/api/workspaces'), ('POST', '/api/workspaces'), ('POST', '/api/tokens'),
        ])
        self.assertEqual(calls[1].args[2], {'email': seed.EMAIL, 'code': '888888'})
        self.assertEqual(calls[-1].kwargs['token'], 'synthetic-jwt')
        self.assertEqual(calls[-1].args[2]['expires_in_days'], 1)
        self.assertEqual(self.pat.read_text(), SYNTHETIC_PAT + '\n')
        self.assertEqual(stat.S_IMODE(self.pat.stat().st_mode), 0o600)
        self.assertNotIn(SYNTHETIC_PAT, logs.getvalue())

    def test_never_overwrites_existing_file_or_dangling_link(self):
        self.pat.symlink_to(self.pat.with_name('absent'))
        with patch.object(seed, 'request') as request:
            with self.assertRaisesRegex(SystemExit, 'existing PAT file'):
                seed.auth(self.pat)
        request.assert_not_called()

    def test_nonempty_workspace_refuses_pat(self):
        with patch.object(seed, 'request', side_effect=[{}, {'token': 'jwt'}, [{'id': WS}]]) as request:
            with self.assertRaisesRegex(SystemExit, 'fresh synthetic account'):
                seed.auth(self.pat)
        self.assertEqual(request.call_count, 3)
        self.assertFalse(self.pat.exists())

    def test_dispatch_binds_agent_issue_and_scope_to_matching_runtime(self):
        with (patch.object(seed, 'synthetic_workspace', return_value=WS),
              patch.object(seed, 'request', side_effect=[
                  [dict(ONLINE, status='offline')],
                  [dict(ONLINE, name='Hermes (other-device)'), ONLINE], [],
                  dict(id=AGENT, workspace_id=WS, runtime_id=RUNTIME),
                  dict(id=ISSUE, workspace_id=WS, assignee_type='agent', assignee_id=AGENT),
              ]) as request, patch.object(seed.time, 'sleep'),
              contextlib.redirect_stdout(io.StringIO())):
            seed.dispatch(SYNTHETIC_PAT)
        calls = request.call_args_list
        self.assertEqual(calls[-2].args[2]['runtime_id'], RUNTIME)
        issue = calls[-1].args[2]
        self.assertEqual(issue['assignee_id'], AGENT)
        self.assertEqual(issue['status'], 'todo')
        self.assertIn(seed.MARKER, issue['description'])
        self.assertTrue(all(c.kwargs == {'token': SYNTHETIC_PAT, 'workspace': WS} for c in calls))

    def test_dispatch_refuses_ambiguous_runtime_or_existing_agent(self):
        for responses, problem in (([[ONLINE, ONLINE]], 'more than one'),
                                   ([[ONLINE], [{'name': seed.AGENT}]], 'already exists')):
            with (self.subTest(problem=problem), patch.object(seed, 'synthetic_workspace', return_value=WS),
                  patch.object(seed, 'request', side_effect=responses) as request):
                with self.assertRaisesRegex(SystemExit, problem):
                    seed.dispatch(SYNTHETIC_PAT)
                self.assertTrue(all(c.args[0] == 'GET' for c in request.call_args_list))

    def test_request_never_follows_redirect_or_reports_response_secret(self):
        rejected = urllib.error.HTTPError(
            seed.API + '/api/tokens', 302, 'redirect', {'Location': 'https://example.invalid'},
            io.BytesIO(json.dumps({'token': SYNTHETIC_PAT}).encode()),
        )
        with patch.object(seed.OPENER, 'open', side_effect=rejected) as opened:
            with self.assertRaisesRegex(SystemExit, 'HTTP 302; response hidden') as exc:
                seed.request('POST', '/api/tokens', {}, token=SYNTHETIC_PAT)
        self.assertNotIn(SYNTHETIC_PAT, str(exc.exception))
        req = opened.call_args.args[0]
        self.assertEqual(req.full_url, 'http://127.0.0.1:18080/api/tokens')
        self.assertTrue(any(isinstance(h, seed.urllib.request.HTTPRedirectHandler)
                            and h.redirect_request(None, None, 302, '', {}, '') is None
                            for h in seed.OPENER.handlers))


if __name__ == '__main__':
    unittest.main()
