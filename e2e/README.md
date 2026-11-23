# One local Multica smoke test

The smoke test covers one synthetic Issue on the pinned Multica v0.11.1
Server/Web, one daemon and the current checkout's Bridge. It needs Docker
Compose v2 and Python 3 on the Docker host. It never needs a model key,
coding agent, production account or real repository.

## Run unattended

From a fresh checkout on a Docker host with Compose v2, Python 3, and ports
13000 and 18080 free, run `make e2e-auto`. It starts a disposable stack,
uses the pinned local development login to mint a one-day PAT for a synthetic
account, starts the daemon, creates and assigns one test Issue, and runs the
same protocol smoke below. It needs no browser, GitHub secret, or existing
Multica account. The stack, test volumes, and local credentials are removed
on exit. It refuses to run over an existing `e2e/.local` directory. Keep the
fixed development verification code on this loopback-only fixture.

GitHub Actions runs this smoke separately from the offline checks, the
Compose supervisor smoke, and the image publisher. Changes limited to this
README do not trigger CI. It tests the pinned self-hosted protocol, not Multica Cloud.

## Bootstrap manually for debugging

1. From a clean checkout on a Docker host with ports 13000 and 18080 free, run:

   ```sh
   ./e2e/run.sh up
   ```

   This pulls the pinned upstream images and builds the Bridge locally. Wait
   for both readiness checks. Do not switch to `latest` if the pinned images
   fail. The test UI is **http://localhost:13000**; the fixture's host API is
   `http://127.0.0.1:18080`, and the daemon uses `http://backend:8080` inside
   Compose. Only loopback ports are published. Do not expose them via a tunnel.
2. In that local UI only, sign up as a synthetic account such as
   `bridge-e2e@example.invalid` using development code `888888`. Create the
   empty workspace **BRIDGE_E2E_ONLY** and a temporary personal access token.
   To reuse it in both commands without pasting twice, put it in the ignored,
   private `e2e/.local/pat` file. In Bash, this reads it without terminal echo:

   ```bash
   (
     umask 077
     IFS= read -rsp 'Synthetic Multica PAT: ' pat
     printf '\n'
     printf '%s\n' "$pat" > e2e/.local/pat
   )
   chmod 600 e2e/.local/pat
   ```

   You can also pass `--token-file /absolute/private/path` to both commands,
   or omit the file and use their hidden interactive prompts. Never put the
   token itself in an argument, chat, fixture output or Git. No production PAT.
3. Run `./e2e/run.sh bootstrap`. In the local UI confirm that
   **synthetic-bridge-e2e** is online and offers **Hermes** (the Bridge
   adapter). Create a synthetic Agent using that runtime. Create one **Todo**
   Issue with `E2E_SYNTHETIC_ONLY` in its title or body, and assign it to that
   Agent. If prompted for local directory access, use the empty container
   path `/workspaces`, never a real project.

## Run the smoke test

```sh
make e2e
# Equivalent, without make: ./e2e/run.sh smoke
```

The command waits up to a minute for one marked local task, confirms the exact Issue/Run
is running in the synthetic workspace, submits one ordinary reply, and
verifies its remote comment belongs to that same Issue, Run and Agent.
It also checks that the Bridge removes the confirmed reply request from Inbox.
It then requests completion and waits for that exact **remote Run** to
report `completed`. Only then does it print `REAL MULTICA E2E PASS`.
It does not close the Issue. If the request is uncertain, inspect it locally
before retrying; do not rerun the smoke blindly.

Record the checkout SHA, upstream image digests, and pass/fail output without
credentials. `make check` is offline and is not Docker or real Multica
evidence. Use `./e2e/run.sh logs` for local diagnosis, and redact any secrets
before sharing logs. Revoke the synthetic token and remove its private file
after testing; `./e2e/run.sh down` stops only this stack and retains test state.
Use a fresh disposable stack and local state for another smoke run.
