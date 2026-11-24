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

    def test_thread_start_after_is_bound_to_exact_parent_and_authorized_input(self):
        owner_id, member_id, parent, denied, allowed, original, following = (
            f'00000000-0000-0000-0000-{i:012d}' for i in range(11, 18))
        owner = dict(token=SYNTHETIC_PAT, workspace=WS)
        member = dict(token='member-jwt', workspace=WS)
        task = dict(issue_id=ISSUE, run_id=original)
        old = dict(id=original, status='running', agent_id=AGENT)
        new = dict(id=following, issue_id=ISSUE, status='running', agent_id=AGENT,
                   workspace_id=WS, trigger_comment_id=allowed, delivered_comment_ids=[allowed])

        def post(source, actor):
            return dict(id=source, issue_id=ISSUE, parent_id=parent, author_type='member',
                        author_id=actor, content='Synthetic thread reply without an explicit mention')

        member_post, owner_post = post(denied, member_id), post(allowed, owner_id)
        before_completion = [[old], member_post, [member_post], [old], owner_post, [old]]
        with (patch.object(seed, 'request', side_effect=before_completion
                           + [[dict(old, status='completed'), new], {}]) as request,
              contextlib.redirect_stdout(io.StringIO()) as logs):
            finish = seed.implicit_follow_up(owner, owner_id, member, member_id, AGENT, task, parent)
            self.assertEqual(request.call_count, 6)
            finish()
        posts = [c for c in request.call_args_list if c.args[0] == 'POST'
                 and c.args[1].endswith('/comments')]
        self.assertEqual(posts[0].args[2], posts[1].args[2])
        self.assertEqual(posts[0].args[2]['parent_id'], parent)
        self.assertNotIn('mention://', posts[0].args[2]['content'])
        self.assertEqual([c.kwargs['token'] for c in posts], ['member-jwt', SYNTHETIC_PAT])
        self.assertTrue(request.call_args.args[1].endswith('/' + following + '/cancel'))
        self.assertIn('THREAD REPLY PROTOCOL E2E PASS:', logs.getvalue())

        with (patch.object(seed, 'request', side_effect=before_completion + [
                  [dict(old, status='completed'), dict(new, delivered_comment_ids=[denied, allowed])]]),
              contextlib.redirect_stdout(io.StringIO()),
              self.assertRaisesRegex(SystemExit, 'denied thread reply entered an upstream Run')):
            seed.implicit_follow_up(owner, owner_id, member, member_id, AGENT, task, parent)()

    def test_restart_follows_upstream_claim_even_if_c_was_already_dispatched(self):
        active, queued, replacement, parent, b, c, d = (
            f'00000000-0000-0000-0000-{i:012d}' for i in range(21, 28))
        owner = dict(token=SYNTHETIC_PAT, workspace=WS)
        scope = dict(issue_id=ISSUE, agent_id=AGENT, workspace_id=WS)
        running = dict(scope, id=active, status='running')
        cancelled = dict(running, status='cancelled')
        pending = dict(scope, id=queued, status='queued', trigger_comment_id=c)

        def posted(source, label):
            return dict(id=source, issue_id=ISSUE, parent_id=parent,
                        content=seed.MARKER + ': restart ' + label)

        prefix = [[running], posted(b, 'B / only comment'), [running],
                  posted(c, 'C / after current Run'), [running, pending], cancelled,
                  posted(d, 'D / restart')]
        for split in (False, True):
            last = replacement if split else queued
            end = dict(scope, id=last, status='running',
                       delivered_comment_ids=[d] if split else [c, d])
            c_run = dict(pending, status='running', delivered_comment_ids=[c])
            responses = ([[cancelled, c_run], [cancelled, dict(c_run, status='completed'), end]]
                         if split else [[cancelled, end]])
            task = dict(issue_id=ISSUE, run_id=queued)
            with (self.subTest(split=split), patch.object(seed, 'request',
                       side_effect=prefix + responses + [dict(end, status='cancelled')]) as request,
                  patch.object(seed, 'expect_run_inputs', return_value=task) as handoff,
                  patch.object(seed, 'submit', return_value='synthetic') as submit,
                  patch.object(seed, 'receipt'), patch.object(seed.time, 'sleep'),
                  contextlib.redirect_stdout(io.StringIO())):
                seed.stop_and_restart(owner, AGENT, ISSUE, active, parent)
                self.assertEqual(request.call_args_list[1].args[2]['suppress_agent_ids'], [AGENT])
                self.assertTrue(request.call_args_list[5].args[1].endswith('/' + active + '/cancel'))
                self.assertTrue(request.call_args_list[6].args[1].endswith('/comments'))
                self.assertEqual(handoff.call_args.args, (ISSUE, last, end['delivered_comment_ids']))
                self.assertEqual(handoff.call_count, 2 if split else 1)
                self.assertEqual(submit.call_count, 1 if split else 0)

    def test_invocation_protocol_compares_same_mention_by_distinct_actors(self):
        owner_id, member_id, invitation_id, denied_id, allowed_id, run_id = (
            f'00000000-0000-0000-0000-{i:012d}' for i in range(5, 11)
        )
        content = f'[@Synthetic private agent](mention://agent/{AGENT}) synthetic invocation check'

        def comment(source, actor, status, reason):
            return dict(id=source, author_type='member', author_id=actor, issue_id=ISSUE,
                        content=content, trigger_outcomes=[dict(target_type='agent',
                        target_id=AGENT, status=status, reason_code=reason)])

        denied = comment(denied_id, member_id, 'blocked', 'invocation_not_allowed')
        allowed = comment(allowed_id, owner_id, 'queued', 'queued')
        run = dict(id=run_id, issue_id=ISSUE, workspace_id=WS, agent_id=AGENT,
                   status='running', trigger_comment_id=allowed_id)
        prefix = [
            dict(id=owner_id), dict(id=member_id), dict(id=invitation_id),
            dict(role='member', user_id=member_id, workspace_id=WS),
            [dict(name=seed.AGENT, id=AGENT, owner_id=owner_id, workspace_id=WS,
                  permission_mode='private')],
            dict(id=ISSUE, workspace_id=WS), [], denied, [denied], [], allowed,
        ]
        with (patch.object(seed, 'synthetic_workspace', return_value=WS),
              patch.object(seed, 'login', return_value='member-jwt') as login,
              patch.object(seed, 'smoke'),
              patch.object(seed, 'request', side_effect=prefix + [
                  [dict(run, delivered_comment_ids=[])],
                  [dict(run, delivered_comment_ids=[allowed_id])],
              ]) as request, patch.object(seed.time, 'sleep'),
              contextlib.redirect_stdout(io.StringIO()) as logs):
            seed.invocation_protocol(SYNTHETIC_PAT)
        login.assert_called_once_with(seed.MEMBER_EMAIL)
        posts = [c for c in request.call_args_list if c.args[0] == 'POST'
                 and c.args[1].endswith('/comments')]
        self.assertEqual([c.args[2]['content'] for c in posts], [content, content])
        self.assertEqual([c.kwargs['token'] for c in posts], ['member-jwt', SYNTHETIC_PAT])
        self.assertIn('INVOCATION PROTOCOL E2E PASS', logs.getvalue())
        self.assertNotIn('member-jwt', logs.getvalue())

        with (patch.object(seed, 'synthetic_workspace', return_value=WS),
              patch.object(seed, 'login', return_value='member-jwt'),
              patch.object(seed, 'smoke'),
              patch.object(seed, 'request', side_effect=prefix + [
                  [dict(run, delivered_comment_ids=[allowed_id, denied_id])],
              ]), contextlib.redirect_stdout(io.StringIO()),
              self.assertRaisesRegex(SystemExit, 'delivered IDs included unauthorized')):
            seed.invocation_protocol(SYNTHETIC_PAT)

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


    def test_attachment_upload_is_multipart_and_returns_stable_path(self):
        attachment_id = '00000000-0000-0000-0000-000000000092'
        raw = json.dumps({'id': attachment_id, 'workspace_id': WS,
                          'filename': seed.ATTACHMENT_NAME, 'size_bytes': len(seed.ATTACHMENT_BYTES),
                          'download_url': 'https://not-followed.example.invalid/signed'}).encode()
        with patch.object(seed.OPENER, 'open', return_value=io.BytesIO(raw)) as opener:
            out = seed.upload_attachment(seed.ATTACHMENT_NAME, seed.ATTACHMENT_BYTES,
                                         token=SYNTHETIC_PAT, workspace=WS, issue_id=ISSUE)
        req = opener.call_args.args[0]
        self.assertEqual(req.full_url, seed.API + '/api/upload-file')
        self.assertEqual(req.method, 'POST')
        self.assertIn('multipart/form-data; boundary=', req.get_header('Content-type'))
        self.assertIn(b'name="file"', req.data)
        self.assertIn(seed.ATTACHMENT_BYTES, req.data)
        self.assertIn(b'name="issue_id"', req.data)
        self.assertIn(ISSUE.encode(), req.data)
        self.assertEqual(out, (attachment_id, '/api/attachments/' + attachment_id + '/download'))


if __name__ == '__main__':
    unittest.main()
