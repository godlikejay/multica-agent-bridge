"""Offline fixture checks, no Docker, network, models, or actual credentials."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import fixture as f


def u():
    return str(uuid.uuid4())


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mb = self.root / "bridge"
        self.patch = patch.multiple(f, ROOT=self.root, MB=self.mb)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def event(self, name):
        e = dict(version=1, kind="task", id="fixture", connection_id=name, workspace_id=u(),
                 issue_id=u(), run_id=u(), epoch=1, body="E2E_SYNTHETIC_ONLY: task", issue_key="TEST-1")
        p = self.mb / "outbox" / name / e["workspace_id"] / "event.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(e))
        return e, p

    def test_init_leaves_supervisor_profile_roots_unbound(self):
        with patch.object(f.os, "getuid", return_value=1000), redirect_stdout(io.StringIO()):
            f.init()
        self.assertEqual(list((self.root / "runtime").iterdir()), [])
        self.assertTrue((self.mb / "inbox" / "a").is_dir())
        cfg = json.loads((self.root / "connections.json").read_text())
        self.assertEqual([x["server_url"] for x in cfg["connections"]],
                         ["http://backend-a:8080", "http://backend-b:8080"])

    def test_accepts_scoped_outbox_and_refuses_incorrect_scope(self):
        a, p = self.event("a")
        self.assertEqual(f.read_task("a", p), a)
        with self.assertRaises(ValueError):
            f.read_task("b", p)
        a["workspace_id"] = u()
        p.write_text(json.dumps(a))
        with self.assertRaises(ValueError):
            f.read_task("a", p)

    def test_refuses_unmarked_or_symlink_task(self):
        a, p = self.event("a")
        link = p.with_name("link.json")
        link.symlink_to(p)
        with self.assertRaises(ValueError):
            f.read_task("a", link)
        a["body"] = "no fixture marker"
        p.write_text(json.dumps(a))
        with self.assertRaises(ValueError):
            f.read_task("a", p)

    def test_outbox_event_must_match_real_server_identity(self):
        a, _ = self.event("a")
        server = dict(id=a["issue_id"], workspace_id=a["workspace_id"],
                      description=r"E2E\_SYNTHETIC\_ONLY", identifier="TEST-1")
        with patch.object(f.single, "get", return_value=server) as get:
            self.assertEqual(f.bound_issue("synthetic", a), server)
            self.assertEqual(f.single.API, "http://127.0.0.1:18180")
            get.assert_called_once_with("/api/issues/" + a["issue_id"], "synthetic")
        server["workspace_id"] = u()
        with patch.object(f.single, "get", return_value=server):
            with self.assertRaises(ValueError):
                f.bound_issue("synthetic", a)

    def test_postponed_scope_action_only_written_once_to_selected_inbox(self):
        b, _ = self.event("b")
        with redirect_stdout(io.StringIO()):
            request = f.action(b, "reply", "synthetic body")
        target = self.mb / "inbox" / "b" / b["workspace_id"] / b["issue_id"] / (request + ".json")
        msg = json.loads(target.read_text())
        self.assertEqual({k: msg[k] for k in f.SCOPE}, {k: b[k] for k in f.SCOPE})
        self.assertEqual(msg["id"], request)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.mb / "inbox" / "a").exists())

    def test_receipt_requires_envelope_and_remote_comment_id(self):
        b, p = self.event("b")
        receipt_id = "synthetic-request"
        receipt = {k: b[k] for k in f.SCOPE}
        receipt.update(request_id=receipt_id, remote_id=u(), status="reply_posted", kind="receipt")
        p.with_name("receipt.json").write_text(json.dumps(receipt))
        self.assertEqual(f.wait_receipt(b, receipt_id, "reply_posted"), receipt)
        with self.assertRaises(ValueError):
            f.wait_receipt(b, receipt_id, "completion_requested")
        receipt["connection_id"] = "a"
        p.with_name("receipt.json").write_text(json.dumps(receipt))
        with self.assertRaises(ValueError):
            f.wait_receipt(b, receipt_id, "reply_posted")

    def test_requires_unique_server_comment_with_run_and_no_cross_reply(self):
        rid, iid, cid = u(), u(), u()
        receipt = dict(run_id=rid, issue_id=iid, remote_id=cid)
        own = dict(source_task_id=rid, issue_id=iid, id=cid, content="marker\n\ncorrelation", author_type="agent")
        f.verify_comment(receipt, "marker", [own], [])
        for a, b in (([], []), ([own, own], []), ([dict(own, source_task_id=u())], []),
                     ([own], [dict(content="marker \n spoof")]), ([dict(own, id=u())], [])):
            with self.assertRaises(ValueError):
                f.verify_comment(receipt, "marker", a, b)

    def test_restart_verification_leaves_other_daemon_running(self):
        a, ap = self.event("a")
        b, bp = self.event("b")
        states = {"a": "running", "b": "running"}
        calls = []
        is_down = False

        def compose(*argv):
            nonlocal is_down
            calls.append(argv)
            if argv == ("stop", "backend-a"):
                is_down = True
            if argv == ("up", "-d", "--no-deps", "backend-a"):
                is_down = False
            return "one-runtime"

        def daemon(name, verb):
            calls.append((name, verb))
            self.assertTrue(is_down)

        def action(event, kind, body):
            name = event["connection_id"]
            if "during-a-outage" in body:
                self.assertTrue(is_down)
                self.assertEqual(name, "b")
                self.assertEqual(states["b"], "running")
            if kind == "complete":
                states[name] = "completed"
            return name + kind

        def verify_status(_, event, expected):
            self.assertEqual(states[event["connection_id"]], expected)

        def restore_pid(name, previous):
            self.assertFalse(is_down)
            self.assertEqual(previous, 11)
            return 12

        def running_pid(name):
            return 22 if name == "b" else None if is_down else 11

        def check_down(name):
            self.assertEqual(name, "a")
            self.assertTrue(is_down)

        def receipt(event, request, status):
            return dict(remote_id=request)

        with patch.multiple(f, compose=compose, daemon=daemon, action=action, verify_comment=lambda *_: None,
                            check_status=verify_status, read_token=lambda x: x, running_pid=running_pid,
                            wait_pid=restore_pid, test_endpoint_down=check_down, wait_receipt=receipt,
                            bound_issue=lambda _, e: {"identifier": e["issue_key"]},
                            wait_completed=lambda t, e: verify_status(t, e, "completed"), comments=lambda *_: []):
            with redirect_stdout(io.StringIO()) as out:
                f.accept(ap, bp)
        self.assertIn("TWO-SERVICE E2E PASS", out.getvalue())
        self.assertIn(("a", "stop"), calls)
        self.assertNotIn(("b", "stop"), calls)
        self.assertEqual(states, {"a": "completed", "b": "completed"})


if __name__ == "__main__":
    unittest.main()
