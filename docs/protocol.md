# File mailbox protocol v1

`BRIDGE_ROOT` is an absolute local path. The operator writes
`inbox/<issue_id>/<request_id>.json`; the Bridge writes shared `outbox/` and
private `state/`. Use one root per authorized Multica workspace/Daemon. Do not let an untrusted process write
these directories, especially the inbox. Use a local filesystem with atomic
rename and advisory file locking; network/sync folders are not supported.

In [multi-connection mode](multi-connection.md), namespace these folders:
`inbox/<connection_id>/<workspace_id>/<issue_id>/<request_id>.json`,
`outbox/<connection_id>/<workspace_id>/<event_id>.json`, and
`state/<connection_id>/<workspace_id>/`. Events and inbox requests in scoped
folders **must** carry the exact `connection_id` and `workspace_id` as JSON
fields. A scoped request is rejected if copied to a different namespace and
the legacy mode does not consume scoped requests. Nothing uses the displayed
Issue key or the project title for identity or routing.

Events and requests are UTF-8 JSON objects in `<id>.json`. To publish an inbox
file, write a temporary file in the same directory and then rename it into place.
IDs use ASCII letters, digits, underscore, dot and hyphen, begin with an
alphanumeric character and are at most 100 characters. IDs are unique within an Issue. Do not reuse an ID with
different bytes; do not overwrite or clear the state of a live mailbox. Files
larger than 256 KiB, links, partial files and malformed input are ignored. Submit
one request at a time **per Issue**, waiting for that action’s receipt.

## Outbox

Fields shared by all messages: `version` (integer 1), `kind`, `id`, `issue_id`,
`run_id` (real Multica task ID), and `epoch` (positive integer, monotonic within an Issue, not globally). The Bridge
derives the binding from the task API and ignores routing instructions in the
ACP prompt. An event is an observation, not permission to act.

- `task`: initial snapshot for the run; `issue_key` and `body` hold display
  metadata and issue text.
- `comment`: `source_id`, `revision`, `body` for a newly observed comment
  version. No deletion events or audit completeness guarantees in this build.
- `receipt`: `request_id`, `status`, and `remote_id` when a posted comment
  was verified. Receipt status describes what the Bridge knows, not the entire
  remote Run state.
- `sync`: a per-Issue binding-scoped observation of remote comment sync;
  `revision` is the Bridge's increasing transition sequence, not chronology of
  comments. `healthy` is the first complete read; `retrying` means sends are
  paused; `recovered` follows a complete read after interruption; `failed` means
  recovery stopped. `observed_at` and `last_synced_at` are UTC timestamps at the
  transition, not a live heartbeat. The Outbox doesn't grow on every poll.

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
  (`blocked_uncertain`) until there is positive evidence of delivery.
- `delivery_confirmed`: a separate receipt for the original request after
  recovery finds its exact comment. It doesn't replace the earlier receipt or
  confirm that the original Run finished. It never finishes a newer Run.

There is no global exactly-once guarantee for remote comments. The local writer
records intent and a unique Markdown correlation marker before posting, then
saves the result. Recovery requires one nondeleted match with the same Issue,
original Run, agent, parent and exact transmitted body/marker. A missing or
ambiguous match (including old requests without markers) remains quarantined.
The marker is delivery metadata, not authentication. The Bridge retries only
transient comment reads with a bounded cancellable delay, never comment posts.
A replacement Run never auto-posts old inbox actions. An unresolved write also
blocks fresh sends on replacement Runs for the same Issue, without affecting
other Issues.

## Concurrency and upgrade

The upstream Daemon admits a bounded number of active Runs. Each creates an
independent ACP worker that resolves its Issue from the Multica task API, takes
a non-blocking Issue lock, and reads only its own Inbox directory. Different
Issues can progress independently; a replacement on the same Issue advances
the epoch and retires the prior Run. The outbox is a collection of immutable
events, not a globally ordered task queue. Completion receipts do not prove that
a remote status is terminal, and task snapshots are not live presence.

Stop the old container before upgrading, with no active Runs. Keep `state/`
and `outbox/` but archive legacy **flat** `inbox/*.json` separately. These flat
files are intentionally ignored; never copy them blindly into a new Issue
directory. Check retained receipts and the remote Issue before preparing new
requests. The JSON envelope remains v1, but operator tools must use the
new directory layout. This protocol does not implement concurrent operators
on different machines, indefinite listening, external schedulers, or automatic
agent invocation.
