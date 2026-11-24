# File mailbox protocol v1

`BRIDGE_ROOT` is an absolute local path, normally the install directory’s
`data/`. The mailbox tool writes authorized requests to the inbox; the
Bridge reads and cleans settled requests from the inbox, and writes the
outbox and private state. Use a local filesystem with atomic rename and
advisory file locking; network/sync folders are not supported. Do not let an
untrusted process write to the mailbox, especially the inbox. Mailbox files
are for delivery, not an archive; consult Multica for remote history.

The [runtime](../deploy/README.md) uses these paths, even for one connection:
`inbox/<connection_id>/<workspace_id>/<issue_id>/<request_id>.json`,
`outbox/<connection_id>/<workspace_id>/<event_id>.json`, and
`state/<connection_id>/<workspace_id>/`. Events and inbox requests in scoped
folders **must** carry the exact `connection_id` and `workspace_id` as JSON
fields. A request is rejected if copied to a different namespace. Nothing
uses the displayed Issue key or the project title for identity or routing.

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
- `comment`: one immutable body per Issue and remote comment revision.
  The Issue body carries `source_id`, `revision`, remote `created_at` when
  provided, and the reserved non-actionable binding `issue-context` / epoch 1.
  A Multica Run input separately has `run_input` (`trigger` or `coalesced`)
  and `body_ref` to the exact local Issue comment event. Its scope, Issue,
  source ID and revision must match the body. Only the authoritative Daemon
  claim receipt (`delivered_comment_ids`) establishes ready inputs for pickup.
  Older responses may expose planned IDs for compatibility, but do not
  declare a ready manifest. No deletion or audit completeness guarantee.
- `receipt`: `request_id`, `status`, and `remote_id` when a posted comment
  was verified. Receipt status describes what the Bridge knows, not the entire
  remote Run state.
- `sync`: a per-Issue binding-scoped observation of remote comment sync;
  `revision` is the Bridge's increasing transition sequence, not chronology of
  comments. `healthy` is the first complete read; `retrying` means sends are
  paused; `recovered` follows a complete read after interruption; `failed` means
  recovery stopped. `observed_at` and `last_synced_at` are UTC timestamps at the
  transition, not a live heartbeat. The Outbox doesn't grow on every poll.
  A successful observation carries `input_ids` only when all authoritative
  Multica claim inputs map to local events for that exact Run and epoch.
  The array holds local Outbox event IDs in claim order; `[]` explicitly
  means no assigned comments. A missing field, unavailable input event, or
  newer degraded observation prevents pickup.

### Local pickup and comment disposal

`take` selects an already delivered Run locally. It does not call Multica,
start work, automatically acknowledge any content, or change write permissions.
The selected Run never silently switches. Remote queue or presence is not
inferred from comments. `comments ISSUE` pages through cached Issue comments
without taking a Run or marking comments read. Use `--context` for only
eligible background during an active Run. Use a new read to pick up newly
synced comments between pages; the same cache is not a remote history API.

Pickup distinguishes exact Run inputs from unread background. Multica's
GET comments does not expose the UI's no-trigger selection. Only an explicit
`/note [mab-context]` is eligible for automatic background in this version:
`/note` suppresses the native trigger, and the second token opts into MAB
context. Plain notes and unclassified comments remain in Issue view only.
Never infer future Run affiliation by timestamp or missing links.

`take` can return a bounded, opaque context cursor to pass back with
`--cursor` on the same Issue. The caller owns it; it is not a server offset,
an acknowledgment, or a credential. A missing, invalid, or mismatched
cursor starts from the compact baseline. It only suppresses already
delivered, unchanged background. Authoritative Run inputs are not suppressed.
For an assignment with no comment input the Issue body is the instruction,
not optional background. An older background revision changing may reset
the compact page so the caller cannot silently miss the edit.

Native attachment metadata includes the original Multica URL and display
name. Automatic context includes Issue-owned attachments and attachments
for comments otherwise eligible for that delivery, not later Run inputs.
No attachment bytes are fetched during pickup. The Bridge refreshes this
metadata separately from immutable task and comment events. A missing
catalog does not block text pickup or mean the Issue has no attachments.

Acknowledge the exact Issue body and exact Run reference separately. The
Bridge removes a read body only once no outstanding Run reference needs it;
a future claim may rehydrate the same body without making it unread again.
Run acknowledgment clears its reference, never an unread Issue body. If
Multica returns a definite cancelled status for an exact prior Run in the
same Issue, agent and workspace, the Bridge can retire its input references
without consuming Issue discussion. An unconfirmed end or missing record
does not retire references; this is not an idle cancellation watcher.
Cleanup runs when an Issue worker is active. No time-based expiry or archive.

Submit an authorized direct reply to a Run input before acknowledging it.
Issue-context records are not direct reply targets. Task snapshots, sync
and operational receipts are not pruned by comment acknowledgment.

## Inbox

Copy the binding from the active task event; never infer it from issue text.

```json
{"version":1,"kind":"reply","id":"fixture-reply-001","issue_id":"issue-fixture","run_id":"run-fixture","epoch":1,"connection_id":"cloud","workspace_id":"workspace-fixture","body":"Synthetic reply"}
```

`reply` posts a comment, but does not finish the run. An optional `parent_id`
must be allowed by the bound run; if omitted a triggering comment is used when
applicable. `complete` posts the final summary and asks ACP to finish normally;
it does not modify Issue status. Neither action interprets body text as code.
Use a nonblank body of at most 32 KiB.

`attachment_fetch` carries `attachment_url` instead of a body or parent.
It uses the active Run's credential to retrieve a native attachment
belonging to that Run's Issue. The caller URL is a reference, not a
network destination; arbitrary Markdown links, other Issues and stale
Run bindings are rejected. A successful receipt has status
`attachment_ready` and a mailbox-root-relative `attachment_path`.
Files are temporary, scoped to the Run, and not a shared download cache.
The consumer can remove them or copy/move them out of Outbox before
completion. Terminal Run cleanup only removes that Run's managed files
in Outbox. A hard process kill can defer cleanup until the next verified
worker start for that Issue. Consumers sharing a mailbox filesystem must
trust each other; use separate mounts for untrusted operators.

For a confirmed reply or final comment, the Bridge persists its delivery
result and publishes a receipt before removing the exact matching Inbox file.
A later positive reconciliation can also settle the original request. An interrupted
cleanup must not resend the request. Failed, unresolved, or conflicting
requests are not automatically discarded. In particular, a confirmed final
comment is not proof that Multica finished the Run: use Multica's status for
that exact Run, not the presence of a newer Run.

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
ambiguous match remains quarantined.
The marker is delivery metadata, not authentication. The Bridge retries only
transient comment reads with a bounded cancellable delay, never comment posts.
A replacement Run never auto-posts old inbox actions. An unresolved write also
blocks fresh sends on replacement Runs for the same Issue, without affecting
other Issues.

## Concurrency and lifecycle

The upstream Daemon admits a bounded number of active Runs. Each creates an
independent ACP worker that resolves its Issue from the Multica task API, takes
a non-blocking Issue lock, and reads only its own Inbox directory. Different
Issues can progress independently; a replacement on the same Issue advances
the epoch and retires the prior Run. The outbox is a collection of immutable
events, not a globally ordered task queue. Completion receipts do not prove that
a remote status is terminal, and task snapshots are not live presence.

Wait for active Runs to finish before upgrades. Keep runtime identity and
private state together, and check receipts and the remote Issue before
retrying an uncertain send. This protocol does not implement concurrent
operators on different machines, indefinite listening, external schedulers,
or automatic agent invocation.
