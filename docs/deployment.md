# Build the Bridge image locally

English | [简体中文](zh-CN/deployment.md)

To run Bridge, follow the [Docker Compose install guide](../deploy/README.md).
The published image already contains Multica Daemon and Bridge. To build your
own image or test runtime changes, use the repository-root [`Dockerfile`](../Dockerfile).

From the checkout:

```sh
make check
docker build -t multica-agent-bridge:local .
docker run --rm --entrypoint /usr/local/bin/multica-bridge multica-agent-bridge:local acp --version
```

For a local image smoke test, prepare a separate directory from the same
checkout:

```sh
test_dir=$(mktemp -d)
cp deploy/compose.yaml "$test_dir/compose.yaml"
cp deploy/connections.example.json "$test_dir/connections.json"
cd "$test_dir"
(umask 077; mkdir -p secrets data/runtime data/inbox data/outbox data/state)
printf 'BRIDGE_UID=%s\nBRIDGE_GID=%s\nBRIDGE_IMAGE=multica-agent-bridge:local\n' "$(id -u)" "$(id -g)" > .env
```

Edit `connections.json` for the test service, then continue at
[Save a token and start](../deploy/README.md#2-save-a-token-and-start).
Use a disposable workspace. To verify the integration against real Multica,
run the [Docker smoke test](../e2e/README.md).

The [image workflow](../.github/workflows/container.yml) publishes Linux
amd64 and arm64 images as `latest` and `sha-<full-git-commit>`. Use a reviewed
digest when the deployed image must be reproducible.

The [Regression workflow](../.github/workflows/regression.yml) calls the
publisher only on `main` after all three checks pass for the same commit.
Only [image-input changes](../.github/scripts/image-inputs.sh) trigger
automatic publication. Changes limited to the Docker E2E fixtures or Python
Skill scripts run checks without publishing. For full path rules and manual
rebuilds, see [contributor verification](../CONTRIBUTING.md#ci-and-image-publication).
