# MAB setup and recovery

Run the bundled Python helper from the project. Read only the relevant section.

## Configure a project

Use `setup /absolute/path/to/data --route CONNECTION:SLUG`. The connection
is named in the operator's install configuration (`connections.json` by
default). Use exact workspace URL slugs; repeat `--route` for multiple workspaces.
Match display names only with trusted workspace information. Setup replaces
the saved routes: when adding one, include the existing verified routes too.

Resolving slugs uses the authenticated CLI in the running Compose Bridge.
Use it only with authorized host access. The install directory defaults to the
parent of the data root; pass `--install-dir /absolute/path/to/install` if
different. With only mailbox access, use verified `CONNECTION:WORKSPACE_UUID`;
it doesn't need Docker. Never guess IDs or read credentials. `scopes` shows
locally observed routes if needed.

Setup saves `.local/mab.json` at the nearest Git worktree root (or current
directory outside Git). A failed match leaves the previous config unchanged.
Report the absolute `config_path` and summarize the saved routes on success.
Keep the file out of Git and rerun setup if the mailbox moves.

## Read more or recover a Run

- If `take` is not ready, wait for sync or inspect exact inputs once available.
  If `truncated` accompanies `read_target`, expand the Issue preview with
  `take TARGET --full` or `read READ_TARGET`. For `background_more`, pass
  the newest returned cursor to the same exact `take TARGET --cursor CURSOR`.
- `take` background contains only local comments opted in with
  `/note [mab-context]`. Untagged comments and future triggers aren't this
  Run's instructions. Use `comments ISSUE` for discussion or
  `comments ISSUE --context` for opted-in context. Pass its `next` as
  `--after NEXT` for subsequent pages; omit it for a fresh unread check.
  If local sync is incomplete, report only what was observed.
- `read EVENT_ID --offset NEXT_OFFSET` continues a body until there is no
  `next_offset`. In a multi-route project, use `--verbose show EVENT_ID`
  to get `connection_id` and `workspace_id`, then place
  `--connection CONNECTION_ID --workspace WORKSPACE_ID` before `read` or
  standalone `ack`. Use `ack EVENT_ID` only when requested and fully read.
  Acknowledging an Issue comment does not acknowledge a separate Run input.

## Check delivery

Add `--wait 35` only if the caller can wait that long. A pending or interrupted
wait doesn't cancel a send or download. If the original request ID was returned,
check it with `result REQUEST_ID`. If not, report uncertainty; do not resubmit
blindly. Confirm final Run state in
Multica if needed. Issue-only discussion targets aren't actionable Run reply
targets. Never silently select another target.
