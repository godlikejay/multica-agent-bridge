"""Synthetic local mailbox and controlling-terminal tests. No Multica access."""

import errno
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).with_name("mailbox.py")
spec = importlib.util.spec_from_file_location("bridge_mailbox_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class MailboxCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.outbox = self.root / "outbox"
        self.inbox = self.root / "inbox"
        self.outbox.mkdir()
        self.inbox.mkdir()
        self.shelf = self.inbox / "issue-1"
        self.event("task-1", "task", body="private fixture issue", issue_key="FIX-1")
        self.event("comment-1", "comment", source_id="source-1", body="雪" * 12000)
        self.body = self.root / "body.txt"
        self.body.write_text("合成回复\x1b[31m\u202e", encoding="utf-8")

    def event(self, event_id, kind, **extra):
        value = dict(version=1, kind=kind, id=event_id, issue_id="issue-1", run_id="run-1", epoch=1)
        value.update(extra)
        (self.outbox / (event_id + ".json")).write_text(json.dumps(value), encoding="utf-8")
        return value

    def invoke(self, *args, success=True, use_env=False):
        command = [sys.executable, "-B", str(SCRIPT)]
        if not use_env:
            command += ["--root", str(self.root)]
        output = subprocess.run(command + [str(a) for a in args], capture_output=True,
                                env={**os.environ, "BRIDGE_ROOT": str(self.root)}, start_new_session=True,
                                input=b"yes\n", timeout=8)
        self.assertEqual(output.returncode == 0, success, output.stderr)
        return json.loads(output.stdout) if success else output.stderr.decode()

    def make_draft(self, event="comment-1", kind="reply"):
        d = self.invoke("draft", event, "--kind", kind, "--body-file", self.body)
        path = self.root / "draft.json"
        path.write_text(json.dumps(d))
        return d, path

    def terminal(self, path, answer, before_answer=None, extra=()):
        pid, terminal = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, "-B", str(SCRIPT), "--root", str(self.root),
                                      "submit", str(path), *extra])
        deadline = time.monotonic() + 8
        output = b""
        answered = False
        exited = False
        try:
            while time.monotonic() < deadline:
                if not select.select([terminal], [], [], max(0, deadline - time.monotonic()))[0]:
                    break
                try:
                    chunk = os.read(terminal, 8192)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    _, status = os.waitpid(pid, 0)
                    exited = True
                    return os.waitstatus_to_exitcode(status), output.decode("utf-8", errors="replace")
                output += chunk
                if not answered and b"anything else cancels" in output:
                    if before_answer:
                        before_answer()
                    os.write(terminal, (answer + "\n").encode())
                    answered = True
            self.fail(f"terminal timed out: {output!r}")
        finally:
            os.close(terminal)
            if not exited:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)

    def test_metadata_and_bounded_read(self):
        (self.outbox / "linked.json").symlink_to("task-1.json")
        self.event("bad-1", [])
        values = self.invoke("list", "--limit", "2", use_env=True)
        self.assertEqual({x["id"] for x in values}, {"task-1", "comment-1"})
        self.assertNotIn("body", values[0])
        self.assertIn("limit", self.invoke("list", "--limit", "0", success=False))
        value = self.invoke("read", "comment-1")
        self.assertTrue(value["body_truncated"])
        self.assertLessEqual(len(value["body"].encode()), cli.MAX_READ_BODY)
        self.assertIn("invalid mailbox ID", self.invoke("read", "../task-1", success=False))
        self.assertEqual(self.invoke("receipt", "not-yet"), [])
        self.event("receipt-1", "receipt", request_id="request-1", status="reply_posted", remote_id="remote-1")
        self.assertEqual(self.invoke("receipt", "request-1")[0]["remote_id"], "remote-1")

    def test_draft_not_live_and_no_controlling_tty(self):
        d, path = self.make_draft()
        self.assertEqual(d["event_id"], "comment-1")
        self.assertEqual(d["request"]["run_id"], "run-1")
        self.assertEqual(d["request"]["parent_id"], "source-1")
        self.assertIn("controlling /dev/tty", self.invoke("submit", path, success=False))
        self.assertFalse(list(self.shelf.glob("*.json")))
        self.assertFalse((self.root / "state").exists())

    def test_tty_full_confirmation_and_no_clobber(self):
        d, path = self.make_draft(kind="complete")
        code, out = self.terminal(path, "not-the-id")
        self.assertEqual(code, 1, out)
        self.assertFalse(list(self.shelf.glob("*.json")))
        request = d["request"]
        code, out = self.terminal(path, request["id"])
        self.assertEqual(code, 0, out)
        self.assertIn('"run_id": "run-1"', out)
        self.assertIn('"kind": "complete"', out)
        self.assertIn("合成回复", out)
        self.assertIn("\\u001b", out)
        self.assertIn("\\u202e", out)
        self.assertNotIn("\x1b", out)
        final = self.shelf / (request["id"] + ".json")
        self.assertEqual(json.loads(final.read_text()), request)
        self.assertEqual(final.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.shelf.iterdir()), [final])
        self.assertEqual(self.shelf.stat().st_mode & 0o777, 0o700)
        self.assertIn("already exists", self.invoke("submit", path, success=False))
        fd = os.open(self.shelf, os.O_RDONLY)
        try:
            with self.assertRaisesRegex(cli.MailboxError, "not overwritten"):
                cli.publish(fd, {**request, "body": "not new"})
        finally:
            os.close(fd)
        self.assertEqual(json.loads(final.read_text()), request)

    def test_rechecks_latest_run_after_operator_wait(self):
        d, path = self.make_draft()
        def replaced():
            self.event("task-2", "task", epoch=2, run_id="run-2")
        code, out = self.terminal(path, d["request"]["id"], replaced)
        self.assertEqual(code, 1, out)
        self.assertIn("stale", out)
        self.assertFalse(list(self.shelf.glob("*.json")))

    def test_rejects_mismatches_pending_and_uncertain(self):
        d, path = self.make_draft()
        bad = {**d, "request": {**d["request"], "run_id": "not-the-bound-run"}}
        path.write_text(json.dumps(bad))
        self.assertIn("does not match", self.invoke("submit", path, success=False))
        path.write_text(json.dumps(d))
        pending = {**d["request"], "id": "pending-1"}
        self.shelf.mkdir(exist_ok=True)
        (self.shelf / "pending-1.json").write_text(json.dumps(pending))
        self.assertIn("unreceipted", self.invoke("submit", path, success=False))
        self.event("uncertain-1", "receipt", request_id="pending-1", status="delivery_uncertain")
        self.assertIn("is uncertain", self.invoke("submit", path, success=False))
        self.event("task-2", "task", epoch=2, run_id="run-2")
        _, new_path = self.make_draft(event="task-2")
        self.assertIn("is uncertain", self.invoke("submit", new_path, success=False))


    def test_filters_and_independent_issue_shelves(self):
        self.event("task-other", "task", issue_id="issue-other", run_id="run-other", epoch=8)
        self.event("comment-other", "comment", issue_id="issue-other", run_id="run-other", epoch=8)
        self.assertEqual({e["id"] for e in self.invoke("list", "--issue-id", "issue-other")},
                         {"task-other", "comment-other"})
        self.assertEqual({e["id"] for e in self.invoke("list", "--run-id", "run-1")},
                         {"task-1", "comment-1"})
        self.assertEqual(self.invoke("list", "--issue-id", "issue-other", "--run-id", "run-1"), [])
        self.assertIn("invalid mailbox ID", self.invoke("list", "--issue-id", "../state", success=False))
        second, second_path = self.make_draft(event="task-other")
        second_id = second["request"]["id"]
        code, out = self.terminal(second_path, second_id)
        self.assertEqual(code, 0, out)
        # Both an unreceipted independent issue and an obsolete root-level
        # exact-ID collision must not block the selected issue shelf.
        first, first_path = self.make_draft(event="task-1")
        first_id = first["request"]["id"]
        legacy = self.inbox / (first_id + ".json")
        legacy.write_text(json.dumps({**first["request"], "body": "obsolete"}))
        code, out = self.terminal(first_path, first_id)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(legacy.read_text())["body"], "obsolete")
        self.assertEqual(json.loads((self.shelf / legacy.name).read_text()), first["request"])
        self.assertEqual(json.loads((self.inbox / "issue-other" / (second_id + ".json")).read_text()),
                         second["request"])

    def test_refuses_issue_links_and_replacement(self):
        item, path = self.make_draft(event="task-1")
        target = self.root / "external-shelf"
        target.mkdir()
        self.shelf.symlink_to(target, target_is_directory=True)
        self.assertIn("links are refused", self.invoke("submit", path, success=False))
        self.shelf.unlink()
        archived = self.inbox / "archived-issue-1"
        def replace_shelf():
            self.shelf.rename(archived)
            self.shelf.mkdir()
        code, output = self.terminal(path, item["request"]["id"], replace_shelf)
        self.assertEqual(code, 1, output)
        self.assertIn("issue inbox changed", output)
        self.assertFalse(list(archived.iterdir()))
        self.assertFalse(list(self.shelf.iterdir()))
        self.assertFalse(list(target.iterdir()))

    def test_read_only_and_draft_do_not_require_inbox(self):
        self.inbox.rmdir()
        self.assertEqual(len(self.invoke("list")), 2)
        self.assertEqual(self.invoke("read", "task-1")["kind"], "task")
        self.assertEqual(self.invoke("receipt", "missing"), [])
        self.make_draft()
        self.assertFalse(self.inbox.exists())


    def test_pickup_explicit_ack_and_no_implicit_open(self):
        self.assertEqual(self.invoke("status", "issue-1")["unread_count"], 2)
        self.invoke("list", "--unread")
        self.assertTrue(self.invoke("read", "comment-1")["body_truncated"])
        self.assertFalse((self.root / "pickup").exists())
        safe_path = str(self.root / "never-opened-worktree")
        draft_path = str(self.root / "never-opened-draft")
        picked = self.invoke("pickup", "issue-1", "--worktree", safe_path, "--draft", draft_path)
        self.assertEqual(picked["worktree"], safe_path)
        self.assertEqual(picked["draft"], draft_path)
        self.assertFalse(Path(safe_path).exists())
        self.assertFalse(Path(draft_path).exists())
        self.assertEqual(self.invoke("ack", "comment-1")["status"], "acknowledged_locally")
        self.invoke("ack", "comment-1")
        self.assertEqual([e["id"] for e in self.invoke("list", "--unread")], ["task-1"])
        self.event("comment-edit", "comment", source_id="source-1", revision="new")
        self.assertEqual(self.invoke("status", "issue-1")["unread_count"], 2)
        self.event("sync-1", "sync", status="retrying", revision="2")
        self.event("sync-2", "sync", status="recovered", revision="3")
        self.assertEqual(self.invoke("status", "issue-1")["latest_sync"]["status"], "recovered")
        self.assertIn("only task/comment", self.invoke("ack", "sync-1", success=False))
        self.assertIn("absolute", self.invoke("pickup", "issue-1", "--draft", "relative", success=False))
        self.invoke("pickup", "issue-1", "--clear-worktree", "--clear-draft")
        rec = self.root / "pickup" / "issue-1.json"
        self.assertEqual(json.loads(rec.read_text())["acknowledged"], ["comment-1"])
        self.assertEqual(rec.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "state").exists())

    def test_sync_recovery_takes_precedence_over_old_failure(self):
        d, path = self.make_draft()
        self.event("sync-10", "sync", status="failed", revision="10")
        self.assertIn("marks this run failed", self.invoke("submit", path, success=False))
        self.event("sync-11", "sync", status="recovered", revision="11")
        os.utime(self.outbox / "sync-10.json", (time.time() + 60, time.time() + 60))
        code, output = self.terminal(path, d["request"]["id"])
        self.assertEqual(code, 0, output)

    def test_comment_parent_checked_before_and_after_tty(self):
        d, path = self.make_draft()
        for parent in (None, "different"):
            wrong = dict(d["request"])
            if parent is None:
                wrong.pop("parent_id")
            else:
                wrong["parent_id"] = parent
            path.write_text(json.dumps({**d, "request": wrong}))
            self.assertIn("reply parent", self.invoke("submit", path, success=False))
        path.write_text(json.dumps(d))
        code, output = self.terminal(path, d["request"]["id"],
                                     lambda: self.event("comment-1", "comment", source_id="changed"))
        self.assertEqual(code, 1, output)
        self.assertIn("reply parent", output)
        self.assertFalse(list(self.shelf.glob("*.json")))
        self.event("comment-1", "comment", source_id="source-1")
        code, output = self.terminal(path, d["request"]["id"])
        self.assertEqual(code, 0, output)
        self.assertIn('"parent_id": "source-1"', output)
        self.assertEqual(json.loads(next(self.shelf.glob("*.json")).read_text())["parent_id"], "source-1")
        self.assertNotIn("parent_id", self.make_draft(kind="complete")[0]["request"])

    def test_rebind_only_confirmed_new_scope(self):
        original, path = self.make_draft()
        saved = path.read_bytes()
        self.event("task-2", "task", run_id="run-2", epoch=2)
        self.event("comment-2", "comment", run_id="run-2", epoch=2, source_id="source-2")
        self.event("task-other", "task", issue_id="issue-other", run_id="run-other", epoch=2)
        self.assertIn("stale", self.invoke("submit", path, success=False))
        self.assertIn("same issue", self.invoke("submit", path, "--rebind", "task-other", success=False))
        self.assertIn("controlling /dev/tty", self.invoke("submit", path, "--rebind", "comment-2", success=False))
        fd = os.open(self.outbox, os.O_RDONLY)
        try:
            revised = cli.rebind_draft(fd, original, "comment-2")["request"]
        finally:
            os.close(fd)
        code, output = self.terminal(path, original["request"]["id"], extra=("--rebind", "comment-2"))
        self.assertEqual(code, 1, output)
        self.assertFalse(list(self.shelf.glob("*.json")))
        code, output = self.terminal(path, revised["id"], extra=("--rebind", "comment-2"))
        self.assertEqual(code, 0, output)
        self.assertIn('"rebind_from"', output)
        self.assertIn('"parent_id": "source-1"', output)
        self.assertIn('"parent_id": "source-2"', output)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(json.loads(next(self.shelf.glob("*.json")).read_text()), revised)
        self.assertIn("already exists", self.invoke("submit", path, "--rebind", "comment-2", success=False))
        self.assertIn("already exists", self.invoke("submit", path, "--rebind", "task-2", success=False))

    def test_rebind_rejects_replay_of_pending_or_posted_old_request(self):
        old, path = self.make_draft()
        self.shelf.mkdir()
        prior = old["request"]
        (self.shelf / (prior["id"] + ".json")).write_text(json.dumps(prior))
        self.event("task-2", "task", run_id="run-2", epoch=2)
        args = ("submit", path, "--rebind", "task-2")
        self.assertIn("pending or unresolved", self.invoke(*args, success=False))
        self.event("confirmed-old", "receipt", request_id=prior["id"], status="reply_posted")
        self.assertIn("may already have posted", self.invoke(*args, success=False))

    def test_scoped_receipt_and_reconciliation(self):
        self.event("r1", "receipt", request_id="same-id", status="delivery_uncertain")
        self.event("r2", "receipt", issue_id="issue-other", request_id="same-id", status="reply_posted")
        self.assertIn("multiple issues", self.invoke("receipt", "same-id", success=False))
        self.event("r3", "receipt", request_id="same-id", status="delivery_confirmed", remote_id="remote-id")
        receipts = self.invoke("receipt", "same-id", "--issue-id", "issue-1", "--run-id", "run-1")
        self.assertEqual([r["status"] for r in receipts], ["delivery_confirmed", "delivery_uncertain"])
        d, path = self.make_draft()
        code, out = self.terminal(path, d["request"]["id"])
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
