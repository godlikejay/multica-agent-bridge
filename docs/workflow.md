# Use your own coding agent without turning your computer into a remote runner

English | [简体中文](zh-CN/workflow.md)

**Keep tasks and results in Multica. Choose when and where the work runs.**
Multica Agent Bridge (MAB) connects Multica to the coding agent you already
use, without asking you to copy task descriptions, screenshots or results
between them.

## Why use a bridge?

When you connect a coding runtime directly to Multica’s Daemon, a Multica
assignment can start that tool on the machine running the Daemon. That is
useful when you want unattended work. Multica has controls for who can use a
runtime, but those controls are separate from what the running tool can reach:
its access depends on the working directory, sandbox and credentials it runs
with. [How Multica runtimes work](https://multica.ai/docs/daemon-runtimes)

**MAB changes what a remote assignment can start.** The Daemon starts a
mailbox worker, not your coding agent. In the [standard deployment](../deploy/compose.yaml),
that worker runs in a container without access to your project checkout,
home directory or Docker socket. It passes the task to a local mailbox and
waits for a response. **You** open your coding agent, choose the project and
permissions, and decide what it may do or send back. An Issue can hand off
work; its text cannot authorize commands or replies.

That separation is the protection. It does not make untrusted Issue content
safe, and it depends on keeping the Bridge’s access narrow and using the
permissions you want in your coding agent.

## Example: the Save button doesn’t work on mobile

1. **Send the task.** In Multica, you file the bug, attach a private screenshot
   and start a Run for the Bridge-backed agent. Your coding agent does not
   start by itself.
2. **Pick it up.** Open your coding agent in the project and say, “Take the
   Save button bug. Look at the screenshot if you need it.” It reads the
   task and asks the Bridge for the screenshot only if needed. The Bridge
   checks that the file belongs to the Issue; you don’t give your coding
   agent a Multica token. The downloaded copy is temporary unless you save it
   elsewhere.
3. **Send the result back.** Once you’ve checked the fix, say, “Post the
   result and finish this round.” The team sees the result on the original
   Issue. Confirm in Multica that the Run finished. The Issue remains open
   for follow-up unless someone changes its status.
4. **Follow up on Safari.** You start another Run on the same Issue:
   “Please also verify iPhone Safari.” The same agent conversation can skip
   background it already read; a fresh conversation gets enough Issue
   context to get started. Either way, the new Safari instruction comes
   through.

[Install MAB](../deploy/README.md) · [Set it up with your coding agent](agent-integration.md)
