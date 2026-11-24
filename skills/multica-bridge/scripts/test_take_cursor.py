"""Offline cursor and attachment mailbox regressions; no network."""
import hashlib
import json
import os
from pathlib import Path
import unittest
import test_mailbox as legacy
cli = legacy.cli


class TakeCursor(legacy.MailboxCLI):
    # Reuse synthetic fixture helpers without inheriting the whole suite.
    def ready(self):
        self.event("ready", "sync", status="healthy", revision="1", input_ids=["input"])
        self.event("input", "comment", run_input="trigger", body="Required instruction " * 200)

    def test_session_delta_and_fallback(self):
        self.ready()
        self.event("background", "comment", run_id="issue-context", discussion="ordinary", body="context")
        first = self.invoke("take", "FIX-1")
        second = self.invoke("take", "FIX-1", "--cursor", first["cursor"])
        self.assertEqual(second["background"], [])
        self.assertNotIn("text", second)
        self.assertEqual(first["inputs"], second["inputs"])
        self.assertLess(len(second["cursor"]), 500)
        for token in ["bad", "x" * 3000]:
            again = self.invoke("take", "FIX-1", "--cursor", token)
            self.assertEqual(again["context"], "reset")
            self.assertEqual(again["background"][0]["text"], "context")
        # Another agent with no cursor still receives the full initial context.
        self.assertIn("text", self.invoke("take", "FIX-1"))
        self.event("task-other", "task", issue_id="issue-2", issue_key="FIX-2")
        self.event("ready-other", "sync", issue_id="issue-2", status="healthy", revision="1", input_ids=[])
        self.assertEqual(self.invoke("take", "FIX-2", "--cursor", first["cursor"])["context"], "reset")

    def test_task_preview_full_and_description_only_instructions(self):
        self.ready()
        self.event("task-1", "task", issue_key="FIX-1", body="context " * 1000)
        initial = self.invoke("take", "FIX-1")
        self.assertEqual(len(initial["text"]), 1800)
        self.assertTrue(initial["truncated"])
        self.assertEqual(initial["read_target"], "task-1")
        self.assertNotIn("task-1", initial["ackable"])
        later = self.invoke("take", "FIX-1", "--cursor", initial["cursor"])
        self.assertNotIn("text", later)
        full = self.invoke("take", "FIX-1", "--cursor", later["cursor"], "--full")
        self.assertEqual(full["text"], "context " * 1000)
        self.event("no-comment-ready", "sync", status="healthy", revision="2", input_ids=[])
        no_input = self.invoke("take", "FIX-1", "--cursor", full["cursor"])
        self.assertEqual(no_input["text_role"], "run_instructions")
        self.assertEqual(no_input["text"], "context " * 1000)

    def test_scope_root_change_and_successor_run(self):
        task = dict(issue_id="issue-1", body="same background", issue_key="FIX-1")
        _, token, _, _ = cli.take_context(None, "/fixture", ("a", "workspace"), task, [])
        for root, scope in [("/other", ("a", "workspace")), ("/fixture", ("b", "workspace")), ("/fixture", ("a", "other"))]:
            self.assertTrue(cli.take_context(token, root, scope, task, [])[3])
        self.ready()
        initial = self.invoke("take", "FIX-1")
        self.event("task-2", "task", issue_key="FIX-1", epoch=2, run_id="run-2", body="private fixture issue")
        self.event("input-2", "comment", epoch=2, run_id="run-2", run_input="trigger", body="new run instruction")
        self.event("ready-2", "sync", epoch=2, run_id="run-2", status="healthy", revision="2", input_ids=["input-2"])
        successor = self.invoke("take", "FIX-1", "--cursor", initial["cursor"])
        self.assertNotIn("text", successor)
        self.assertEqual(successor["inputs"][0]["text"], "new run instruction")

    def test_attachment_only_change_and_large_metadata(self):
        task = dict(issue_id="issue-1", body="background", issue_key="FIX-1")
        catalog = {"issue": [{"url": "/api/attachments/a", "filename": "a"}], "comments": {}}
        _, token, _, _ = cli.take_context(None, "/fixture", (), task, [], catalog=catalog)
        self.assertFalse(cli.take_context(token, "/fixture", (), task, [], catalog=catalog)[2])
        catalog["issue"].append({"url": "/api/attachments/b", "filename": "b"})
        self.assertTrue(cli.take_context(token, "/fixture", (), task, [], catalog=catalog)[2])
        self.assertEqual(len(cli.attachment_links(catalog["issue"] * 60)), 120)

    def test_background_more_reports_undelivered_page(self):
        self.ready()
        for i in range(6):
            self.event("bg-" + str(i), "comment", run_id="issue-context", discussion="ordinary", body=str(i))
        first = self.invoke("take", "FIX-1")
        self.assertEqual(len(first["background"]), 4)
        self.assertTrue(first["background_more"])
        second = self.invoke("take", "FIX-1", "--cursor", first["cursor"])
        self.assertEqual(len(second["background"]), 2)
        self.assertNotIn("background_more", second)

    def test_partial_context_and_backdated_revision(self):
        self.ready()
        body = "雪" * 1300
        self.event("long-bg", "comment", run_id="issue-context", source_id="source-bg", discussion="ordinary", body=body)
        result = self.invoke("take", "FIX-1")
        pages = [result["background"][0]["text"]]
        while result["background"] and result["background"][0].get("truncated"):
            result = self.invoke("take", "FIX-1", "--cursor", result["cursor"])
            pages += [result["background"][0]["text"]]
        self.assertEqual("".join(pages), body)
        cursor = result["cursor"]
        file = self.outbox / "long-bg.json"
        previous = file.stat().st_mtime_ns
        self.event("long-bg", "comment", run_id="issue-context", source_id="source-bg", revision="2", discussion="ordinary", body="edited old context")
        os.utime(file, ns=(previous, previous))
        revised = self.invoke("take", "FIX-1", "--cursor", cursor)
        self.assertEqual(revised["context"], "reset")
        self.assertEqual(revised["background"][0]["text"], "edited old context")

    def test_ack_does_not_filter_existing_inputs_and_missing_fails(self):
        self.ready()
        take = self.invoke("take", "FIX-1")
        self.invoke("ack-take", "task-1", "input")
        self.assertEqual(self.invoke("take", "FIX-1", "--cursor", take["cursor"])["inputs"], take["inputs"])
        (self.outbox / "input.json").unlink()
        self.assertEqual(self.invoke("take", "FIX-1")["status"], "input_unavailable")

    def test_attachment_url_byte_limit_and_unavailable_catalog(self):
        self.ready()
        self.invoke("take", "FIX-1")
        self.assertIn("original native", self.invoke("attachment", "https://example.test/" + "雪" * 1400, "--wait", "0", success=False))
        directory = self.outbox / "attachments" / "issue-1" / "run-1" / "1"
        directory.mkdir(parents=True)
        (directory / "catalog.json").write_text(json.dumps(dict(version=1, issue_id="issue-1", run_id="run-1", epoch=1, status="unavailable")))
        take = self.invoke("take", "FIX-1")
        self.assertEqual(take["status"], "ready")
        self.assertNotIn("attachments", take)
        with self.assertRaises(cli.MailboxError):
            cli.attachment_links([{"url": "https://example.test/" + "雪" * 1400}])

    def test_attachment_catalog_request_result_and_safe_file(self):
        self.ready()
        directory = self.outbox / "attachments" / "issue-1" / "run-1" / "1"
        directory.mkdir(parents=True)
        catalog = dict(version=1, issue_id="issue-1", run_id="run-1", epoch=1, status="ready",
                       issue=[dict(url="/api/attachments/native", filename="参考.png")], comments={})
        (directory / "catalog.json").write_text(json.dumps(catalog))
        taken = self.invoke("take", "FIX-1")
        self.assertEqual(taken["attachments"], [{"url": "/api/attachments/native", "name": "参考.png"}])
        self.event("other-task", "task", issue_id="issue-2", issue_key="FIX-2")
        self.event("other-ready", "sync", issue_id="issue-2", status="healthy", revision="1", input_ids=[])
        self.invoke("take", "FIX-2")  # another agent changed shared selection
        pending = self.invoke("attachment", "/api/attachments/native", "--run", "task-1", "--wait", "0")
        self.assertEqual(pending["status"], "pending")
        rid = pending["request"]
        req = json.loads((self.shelf / (rid + ".json")).read_text())
        self.assertEqual(req["kind"], "attachment_fetch")
        self.assertEqual(req["issue_id"], "issue-1")
        self.assertNotIn("body", req)
        (directory / rid).write_bytes(b"synthetic attachment")
        receipt = hashlib.sha256("\0".join(["receipt", "issue-1", rid]).encode()).hexdigest()
        rel = f"outbox/attachments/issue-1/run-1/1/{rid}"
        self.event(receipt, "receipt", request_id=rid, status="attachment_ready", attachment_path=rel)
        result = self.invoke("result", rid)
        self.assertEqual(result["path"], str(directory / rid))
        (directory / (rid + ".png")).write_bytes(b"synthetic image")
        self.event(receipt, "receipt", request_id=rid, status="attachment_ready", attachment_path=rel + ".png")
        self.assertEqual(self.invoke("result", rid)["path"], str(directory / (rid + ".png")))
        self.event(receipt, "receipt", request_id=rid, status="attachment_ready", attachment_path=rel + ".exe")
        self.assertIn("does not match", self.invoke("result", rid, success=False))
        self.event(receipt, "receipt", request_id=rid, status="attachment_ready", attachment_path=rel)
        (directory / rid).unlink()
        (directory / rid).symlink_to(self.body)
        self.assertIn("mailbox", self.invoke("result", rid, success=False))
        self.event(receipt, "receipt", request_id=rid, status="attachment_ready", attachment_path="../../escape")
        self.assertIn("does not match", self.invoke("result", rid, success=False))

# Do not re-run inherited legacy tests here (they run in test_mailbox.py).
for name in list(vars(legacy.MailboxCLI)):
    if name.startswith("test_") and name not in vars(TakeCursor):
        setattr(TakeCursor, name, None)

if __name__ == "__main__":
    unittest.main()
