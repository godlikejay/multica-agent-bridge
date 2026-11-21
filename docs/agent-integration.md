# Use the mailbox from a coding agent

Use the same [portable skill](../skills/multica-bridge/SKILL.md) for Codex,
Antigravity CLI (`agy`), or Oh My Pi (`omp`). There is no per-agent service or
MCP server. The folder contains the instructions and a Python 3 standard-library
mailbox tool; the agent and your terminal need local access to the mailbox.

## Install one copy

From a checkout of this repository, copy the **whole** directory into the
project where the agent works. Keep its folder name and contents unchanged:

```sh
mkdir -p /path/to/project/.agents/skills
cp -R skills/multica-bridge /path/to/project/.agents/skills/
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

## The operator's mailbox workflow

From the Bridge checkout, for example:

```sh
export BRIDGE_ROOT="$PWD/.bridge"
tool=skills/multica-bridge/scripts/mailbox.py
python3 "$tool" list --issue-id ISSUE_ID  # Omit the filter to find task IDs.
python3 "$tool" pickup ISSUE_ID --worktree /absolute/path/to/checkout
python3 "$tool" status ISSUE_ID
python3 "$tool" list --issue-id ISSUE_ID --unread
python3 "$tool" read EVENT_ID
python3 "$tool" ack EVENT_ID  # Explicitly mark a reviewed task/comment.
python3 "$tool" draft EVENT_ID --kind reply --body-file reply.txt > reply.draft.json
python3 "$tool" pickup ISSUE_ID --draft "$PWD/reply.draft.json"
# Run this yourself, in a terminal, after reviewing destination and exact text.
python3 "$tool" submit reply.draft.json
python3 "$tool" receipt REQUEST_ID --issue-id ISSUE_ID
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

A draft is not an Inbox request. The operator's `submit` validates scope against
the available local events, shows exactly what will be sent, and asks for
confirmation before atomically creating the file under `inbox/<issue_id>/`. The Bridge independently
checks the active binding. This is not a security boundary against other
processes with identical filesystem and terminal privileges. To enforce a
human-only write boundary, restrict the agent to outbox and a scratch/draft
directory, and keep the inbox and approval process under a different identity.

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
