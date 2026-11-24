# Multica Agent Bridge

English | [简体中文](README.zh-CN.md)

A file mailbox between a [Multica](https://github.com/multica-ai/multica) issue run
and a local operator. The bridge uses Multica's Hermes ACP interface without
running a model. It does not launch or control an agent, open a checkout, or
execute instructions from issue text.

This repository is an early prototype with one writer per Issue. Do not connect it to a
team or production workspace until the disposable integration suite passes and
the operating permissions have been reviewed.

## Install with Docker Compose

You need Docker Compose v2 and access to an existing Multica service. The
runtime image includes Multica Daemon and Bridge. You do not need Go, a model
API key, or a separate Daemon installation. The host mailbox tool uses
Python 3 (standard library only).

- **Use the published image (recommended):** [Install without cloning](deploy/README.md).
  Download two config files, save your Multica token, then run `docker compose up -d`.
- **Build your own image:** Use the repository-root [`Dockerfile`](Dockerfile).
  Run `docker build -t multica-agent-bridge:local .` from the source checkout,
  then set `BRIDGE_IMAGE=multica-agent-bridge:local` in your install directory's
  `.env`. See the [build guide](docs/deployment.md) for the full steps.

Both options use the same Compose configuration for one or more connections.

## Manage connections with a short command

With an image that supports connection control, run this in the Compose
directory to make `mab` available in your current shell:

```sh
mab() { docker compose exec -T bridge multica-bridge connection "$@"; }
```

Use `mab status`, `mab stop company`, or `mab start company`; replace
`company` with the connection ID. Stopping can interrupt active Runs.
For a shortcut that works from any directory, see the
[one-time setup](deploy/README.md#set-up-the-mab-shortcut). You can ask a
coding agent on the Docker host to do that setup for you.

## Use it from your coding agent

Once the Bridge is online, [install the MAB Skill](docs/agent-integration.md#install-one-copy)
for Codex, Antigravity CLI, or Oh My Pi. Open the coding agent in your project
and ask it to configure MAB once:

> Set up Multica for this project. My Bridge mailbox is at
> `/path/to/bridge/data`. Follow `engineering` and `operations` on the
> `cloud` connection.

`cloud` is a connection ID from your install; the workspace names in this
example are URL slugs. You can follow multiple workspaces across different
connections. The agent saves the project settings in `.local/mab.json` and
tells you the file path. Do not give the agent your Multica token.

Then talk to the agent normally, without repeating the path or workspaces:

- “Check this project’s Multica tasks.”
- “Read OPS-123, reply that we’ve received it, and share your initial take.”
- “Any new comments on OPS-123? Should we change the plan?”
- “Take the next run for OPS-123 and look at the attached screenshot if needed.”
- “OPS-123 is finished. Post a short summary and end this run.”

The Skill can pass a context cursor between takes in the same conversation
to avoid rereading Issue background. Attachments download only on request;
keep anything you need after the Run outside the temporary Outbox.

The agent sees what MAB has received, not Multica’s full queue. Ending
a Run does not close the Issue. See the [agent guide](docs/agent-integration.md)
for setup and manual alternatives.

## Scope

The standard Multica daemon owns the remote Run. An issue and observed comment
versions appear in the outbox. Replies and requests to finish a Run need
local authorization; remote issue text cannot authorize either. Finishing a
Run does not change its Issue status. Multiple Issues can be active. Runs for
one Issue are serialized. A new Run for that Issue increments its epoch; old
requests are not carried over.

The mailbox and private Bridge state are JSON files. The runtime reads the
Issue-specific Inbox. An agent that submits authorized actions needs Inbox
write access; the Skill does not enforce OS permissions.

Keep each Issue's local pickup state (reviewed messages, checkout and unsent
draft reference) separate from its live Run and from other Issues. The pickup
tool does not start agents, change Multica, or turn a draft into an approved
reply. See [the operator workflow](docs/agent-integration.md).

## Licenses

Bridge source is licensed under [Apache-2.0](LICENSE). The runtime image is
built on [Multica](https://github.com/multica-ai/multica) and bundles its
v0.11.1 CLI/Daemon under the [Multica License](https://github.com/multica-ai/multica/blob/v0.11.1/LICENSE).
The image includes both components' license files and the upstream
[Multica NOTICE](https://github.com/multica-ai/multica/blob/v0.11.1/NOTICE)
under `/usr/share/licenses/`.
