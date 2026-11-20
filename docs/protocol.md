# File mailbox protocol v1

`BRIDGE_ROOT` is an absolute local path. An operator writes `inbox/`; the Bridge
writes `outbox/` and private `state/`. Do not let an untrusted process write
these directories, especially the inbox. Use a local filesystem with atomic
rename and advisory file locking; network/sync folders are not supported.

Events and requests are UTF-8 JSON objects in `<id>.json`. To publish an inbox
file, write a temporary file in the same directory and then rename it into place.
IDs use ASCII letters, digits, underscore, dot and hyphen, begin with an
alphanumeric character and are at most 100 characters. Do not reuse an ID with
different bytes; do not overwrite or clear the state of a live mailbox. Files
larger than 256 KiB, links, partial files and malformed input are ignored. Send
one request at a time and wait for a receipt before submitting another.

## Outbox

Fields shared by all messages: `version` (integer 1), `kind`, `id`, `issue_id`,
`run_id` (real Multica task ID), and `epoch` (positive integer). The Bridge
derives the binding from the task API and ignores routing instructions in the
ACP prompt. An event is an observation, not permission to act.

- `task`: initial snapshot for the run; `issue_key` and `body` hold display
  metadata and issue text.
- `comment`: `source_id`, `revision`, `body` for a newly observed comment
  version. No deletion events or audit completeness guarantees in this build.
- `receipt`: `request_id`, `status`, and `remote_id` when a posted comment
  was verified. Receipt status describes what the Bridge knows, not the entire
  remote Run state.

## Inbox

Copy the binding from the active task event; never infer it from issue text.

```json
{"version":1,"kind":"reply","id":"fixture-reply-001","issue_id":"issue-fixture","run_id":"run-fixture","epoch":1,"body":"Synthetic reply"}
```

`reply` posts a comment, but does not finish the run. An optional `parent_id`
must be allowed by the bound run; if omitted a triggering comment is used when
applicable. `complete` posts the final summary and asks ACP to finish normally;
it does not modify Issue status. Neither action interprets body text as code.
Use a nonblank body of at most 32 KiB.

Receipt statuses:

- `reply_posted`: the issue comment API returned a matching comment.
- `completion_requested`: final comment accepted and local completion
  persisted. Confirm remote Run completion in Multica; this is not proof of it.
- `stale`, `id_conflict`, `invalid_parent`, `invalid_body`, `remote_rejected`: request rejected.
- `delivery_uncertain`: a write cannot be confirmed. Do not resend blindly.
  Inspect the actual Issue and Run; further sends for this Run are blocked
  (`blocked_uncertain`). This prototype has no automated reconciliation or
  resolution command.

There is no global exactly-once guarantee for remote comments. The local writer
records intent before posting and saves the result; if it crashes between those
steps the recovered state is uncertain. A replacement Run never auto-posts old
inbox actions. This protocol does not implement multiple writers, indefinite
listening, external schedulers, or automatic agent invocation.
