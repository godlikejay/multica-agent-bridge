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

    def terminal(self, path, answer, before_answer=None):
        pid, terminal = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, "-B", str(SCRIPT), "--root", str(self.root),
                                      "submit", str(path)])
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
        self.assertNotIn("parent_id", d["request"])
        self.assertIn("controlling /dev/tty", self.invoke("submit", path, success=False))
        self.assertFalse(list(self.inbox.iterdir()))
        self.assertFalse((self.root / "state").exists())

    def test_tty_full_confirmation_and_no_clobber(self):
        d, path = self.make_draft(kind="complete")
        code, out = self.terminal(path, "not-the-id")
        self.assertEqual(code, 1, out)
        self.assertFalse(list(self.inbox.iterdir()))
        request = d["request"]
        code, out = self.terminal(path, request["id"])
        self.assertEqual(code, 0, out)
        self.assertIn('"run_id": "run-1"', out)
        self.assertIn('"kind": "complete"', out)
        self.assertIn("合成回复", out)
        self.assertIn("\\u001b", out)
        self.assertIn("\\u202e", out)
        self.assertNotIn("\x1b", out)
        final = self.inbox / (request["id"] + ".json")
        self.assertEqual(json.loads(final.read_text()), request)
        self.assertEqual(final.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.inbox.iterdir()), [final])
        self.assertIn("already exists", self.invoke("submit", path, success=False))
        fd = os.open(self.inbox, os.O_RDONLY)
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
        self.assertFalse(list(self.inbox.iterdir()))

    def test_rejects_mismatches_pending_and_uncertain(self):
        d, path = self.make_draft()
        bad = {**d, "request": {**d["request"], "run_id": "not-the-bound-run"}}
        path.write_text(json.dumps(bad))
        self.assertIn("does not match", self.invoke("submit", path, success=False))
        path.write_text(json.dumps(d))
        pending = {**d["request"], "id": "pending-1"}
        (self.inbox / "pending-1.json").write_text(json.dumps(pending))
        self.assertIn("unreceipted", self.invoke("submit", path, success=False))
        self.event("uncertain-1", "receipt", request_id="pending-1", status="delivery_uncertain")
        self.assertIn("uncertain run", self.invoke("submit", path, success=False))


if __name__ == "__main__":
    unittest.main()
