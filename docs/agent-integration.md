# Use the mailbox from a coding agent

English | [简体中文](zh-CN/agent-integration.md)

Use the same [portable skill](../skills/multica-bridge/SKILL.md) for Codex,
Antigravity CLI (`agy`), or Oh My Pi (`omp`). There is no per-agent service or
MCP server. The folder contains the instructions and a Python 3 standard-library
mailbox tool; the agent and your terminal need local access to the mailbox.

## Install one copy

From the project where the agent works, download the Skill and its helper.
Use the same source revision as your image if you pinned the image by commit:

```sh
skill=.agents/skills/multica-bridge
src=https://raw.githubusercontent.com/godlikejay/multica-agent-bridge/main/skills/multica-bridge
mkdir -p "$skill/scripts"
curl -fsSLo "$skill/SKILL.md" "$src/SKILL.md"
curl -fsSLo "$skill/scripts/mailbox.py" "$src/scripts/mailbox.py"
```

Do not add local mailbox paths, logs, drafts or credentials to that project.
For a personal install, the reference locations and explicit invocation are:

| Agent | Personal skill path | Invoke explicitly |
| --- | --- | --- |
| [Codex](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills) | `~/.agents/skills/multica-bridge/` | `$multica-bridge` |
| [Antigravity CLI](https://antigravity.google/docs/skills#cli-skill-locations) | `~/.gemini/config/skills/multica-bridge/` | `/multica-bridge`; use `/skills reload` if needed |
| [Oh My Pi](https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md) | `~/.omp/agent/skills/multica-bridge/` | `/skill:multica-bridge` when skill commands are enabled |

No client-specific wrapper is required. Loading a Skill does not grant access,
approve an action, or start a task. Use the same CLI directly without an agent.

## Configure once per project

Talk to the agent normally: “Follow the Engineering and Operations workspaces
on our company connection,” “What tasks has this project received?”, “Reply to
OPS-123 that the fix is ready,” or “Finish this run for OPS-123.” The agent
looks up the routing details and runs the tool. It asks for confirmation to
finish a run, which does not close the Issue. You can give workspace names or
URL slugs in conversation; a slug from the workspace URL is the simplest
way to identify it exactly.
After setup, the agent reports the exact path of the file it changed and
summarizes what changed so you can review it later.

On the machine where the agent will use the Skill, point its helper to the
absolute path of the [install directory's](../deploy/README.md) `data/` (or
your configured `BRIDGE_DATA_DIR`). The path must exist on that machine.
On the Bridge host, set routes with the connection ID from `connections.json`
and the exact workspace slug from its Multica URL. Repeat `--route` for a
project with multiple workspaces. You can do this before the first task arrives.

```sh
python3 "$skill/scripts/mailbox.py" setup /absolute/path/to/install/data \
  --route cloud:engineering --route cloud:operations
python3 "$skill/scripts/mailbox.py" list --unread
```

With a slug, setup queries the running Compose Bridge using the authenticated
Multica CLI for that connection. It needs no new credentials. It looks for
an exact slug and saves its UUID; if a route cannot be resolved, the saved
configuration is not changed. By default the helper looks for the Compose
installation in the parent of `data/`. If the data and Compose directories
are separate, add `--install-dir /absolute/path/to/compose/install` to setup.
If you're configuring from another machine or without access to the running
Compose service, use `--route CONNECTION:WORKSPACE_UUID` instead; a UUID
needs no lookup. Task and reply commands work as before and do not need Docker.

Use a separate project copy of the Skill when projects have different routes.
Setup saves the path and routes in `mailbox.local.json` beside the installed
`SKILL.md`; add it to your project's Git ignore rules. Never put a token in
that file. Rerun setup with the full route list if paths or routes change.

Without any `--route`, setup saves only the mailbox path. Use `scopes` to
discover connection/workspace pairs already present in that mailbox, or
troubleshoot routing; then either save routes or pass the exact pair for
each command. `scopes` is not needed for day-to-day work.

An explicit `--root` overrides the saved project path. `BRIDGE_ROOT` is a
fallback when no project path is saved.
A different mailbox root does not inherit the saved routes. At the saved
root, explicit selectors outside the configured project routes are rejected.

## The operator's mailbox workflow

`list` and `list --unread` cover all routes configured for the project. Each
record includes `connection_id` and `workspace_id`. With one saved route you
can omit both selectors for read, draft, submit and related commands. With
multiple routes, take the exact pair from the selected list record and pass
`--connection ID --workspace UUID` before the subcommand. Use the same pair
for every operation on that task.

```sh
tool=/absolute/path/to/project/.agents/skills/multica-bridge/scripts/mailbox.py
mailbox() { python3 "$tool" "$@"; }
mailbox list --unread
# For multiple routes, fill these two values from the chosen list record.
# With one route, this function may omit --connection and --workspace.
task_mailbox() { mailbox --connection CONNECTION_ID --workspace WORKSPACE_UUID "$@"; }
task_mailbox list --issue-id ISSUE_ID  # Omit the filter to find task IDs.
task_mailbox pickup ISSUE_ID --worktree /absolute/path/to/checkout
task_mailbox status ISSUE_ID
task_mailbox list --issue-id ISSUE_ID --unread
task_mailbox read EVENT_ID
task_mailbox ack EVENT_ID  # Explicitly mark a reviewed task/comment.
task_mailbox draft EVENT_ID --kind reply --body-file reply.txt > reply.draft.json
task_mailbox pickup ISSUE_ID --draft "$PWD/reply.draft.json"
# Run this yourself, in a terminal, after reviewing destination and exact text.
task_mailbox submit reply.draft.json
task_mailbox receipt REQUEST_ID --issue-id ISSUE_ID
```

`list` shows metadata, not the full message backlog. Pick the actual event and
its issue/run/epoch before drafting. The local list is not a complete queue of
all Issues assigned in Multica. Queued Runs appear locally only once picked up.
Use `--issue-id` or `--run-id` to narrow the list; a task event is not proof
that the Run is still active on the server.

`pickup` records local notes for one Issue: a checkout path and/or an unsent
draft path. It does not open those paths or start work. `status` summarizes the
Issue's local progress and last observed sync, not live Multica state. `read` and
`list` don't mark anything reviewed; acknowledge only after examining the
content you need. An edited comment is a new event even if the previous edit
was acknowledged. Keep pickup state and draft files out of the project repo.

A draft is not an Inbox request. By default, `submit` previews the destination
and text, then asks for terminal confirmation. If you explicitly authorize
an ordinary reply to the exact task, the agent can run
`task_mailbox submit reply.draft.json --yes` with the same connection and workspace
selectors. `--yes` skips only the terminal prompt, not the binding and content
checks. Completing a Run or rebinding a draft always requires terminal
confirmation. A request in an Issue or comment is not authorization to send.

Give the agent only the mailbox access it needs: Outbox read access, and Inbox
write access if it will submit authorized replies. The tool writes requests
atomically and the Bridge independently checks the active binding. To enforce
human-only submission, deny the agent Inbox write access and keep terminal
approval under a separate identity. Never share daemon credentials or the
private Bridge state.

If Multica starts a replacement Run, read the new task/comment first. To reuse
the old text, the operator can run `submit reply.draft.json --rebind NEW_EVENT_ID`.
This previews a new request tied to the newer Run for the **same Issue**, and
requires fresh approval. It leaves the old draft file untouched. For a reply
draft based on a comment, confirm the exact parent; the Bridge will reject
it if the Run cannot reply to that thread. A delivery still marked uncertain
must not be resent just because no receipt or comment is visible.

Each explicit `complete` is scoped to one Run. It doesn't close the corresponding
Issue or change other tasks. Check the actual remote Run in Multica; if delivery
is uncertain, inspect the issue before any further action.
