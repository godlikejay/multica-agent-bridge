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

A shared or symlinked install works across projects; run the helper from the
project you want to use. No client-specific wrapper is required. Loading a Skill does not grant access,
approve an action, or start a task. Use the same CLI directly without an agent.

## Configure once per project

Talk to the agent normally: “Follow the Engineering and Operations workspaces
on our company connection,” “What tasks has this project received?”, “Reply to
OPS-123 that the fix is ready,” or “Finish this run for OPS-123.” The agent
looks up the routing details and runs the tool. Finishing the Run on request
does not close the Issue. You can give workspace names or
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
python3 "$skill/scripts/mailbox.py" tasks
```

With a slug, setup queries the running Compose Bridge using the authenticated
Multica CLI for that connection. It needs no new credentials. It looks for
an exact slug and saves its UUID; if a route cannot be resolved, the saved
configuration is not changed. By default the helper looks for the Compose
installation in the parent of `data/`. If the data and Compose directories
are separate, add `--install-dir /absolute/path/to/compose/install` to setup.
If you're configuring from another machine or without access to the running
Compose service, use `--route CONNECTION:WORKSPACE_UUID` instead; a UUID
needs no lookup. Day-to-day task and reply commands do not need Docker.

Run setup from the project. It saves the path and routes in `.local/mab.json`
at the nearest Git worktree root, or in the current directory outside a Git
worktree. The helper uses the project location, not the Skill's location,
so a shared or symlinked Skill does not share your settings. Add
`/.local/mab.json` to the project's Git ignore rules. Never put a token in
the file. Rerun setup with the full route list if paths or routes change.
When invoking the helper from elsewhere, pass `--project /absolute/path/to/project`
before the subcommand.

Without any `--route`, setup saves only the mailbox path. Use `scopes` to
discover connection/workspace pairs already present in that mailbox, or
troubleshoot routing; then either save routes or pass the exact pair for
each command. `scopes` is not needed for day-to-day work.

An explicit `--root` overrides the saved project path. `BRIDGE_ROOT` is a
fallback when no project path is saved.
A different mailbox root does not inherit the saved routes. At the saved
root, explicit selectors outside the configured project routes are rejected.

## Use the mailbox day to day

Ask the agent naturally, for example: “What Multica tasks have come in?”, “Read
OPS-123 and summarize the latest requests,” or “Reply to OPS-123 that the fix
is ready.” It uses the saved project workspaces without asking you for IDs or
flags. If an issue key matches multiple workspaces, the tool refuses to guess.

To use the same compact interface directly from a terminal:

```sh
tool=.agents/skills/multica-bridge/scripts/mailbox.py
python3 "$tool" tasks
python3 "$tool" tasks OPS-123
python3 "$tool" show OPS-123
# Use the exact target returned by show, not the issue key, for an authorized write.
python3 "$tool" reply TARGET 'The fix is ready.'
# Use complete instead of reply when the operator has asked to end this Run.
python3 "$tool" complete TARGET 'The fix is complete; checks passed.'
# Only if the action is pending, check the request ID it returned.
python3 "$tool" result REQUEST_ID
```

`tasks` summarizes the latest Run MAB received per Issue. It is not the whole
Multica queue or live Run status. `show` expands one Issue or an exact event.
For a reply to a particular comment, use the target for that comment. New
Runs are not visible locally until handed to MAB; old replies and drafts
never authorize work or a write on a new Run.

`reply` and `complete` submit actions immediately, so use them only where the
operator explicitly approved the action. Both validate the exact event and
Run binding. For a preview or manual approval, the separate `draft` and
`submit` commands remain available; rebinding a draft requires terminal
confirmation. The Bridge independently validates the binding before sending.
If delivery is uncertain, check the original Issue or wait for reconciliation;
do not resend automatically. A completion receipt means the request was
accepted locally, not that the Run ended in Multica. Completing a Run does
not close the Issue.

Give the agent Outbox access and Inbox write permission only if it should
send authorized actions. For human-only submission, deny it Inbox write
access and keep approval under a separate identity. Do not share daemon
credentials or private Bridge state.
