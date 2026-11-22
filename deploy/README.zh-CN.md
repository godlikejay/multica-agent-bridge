# 用现成镜像安装

[English](README.md) | 简体中文

只需要 Docker Compose 和现有的 Multica 账号，不用 clone 仓库、编译或另装 daemon。支持 x86-64 Linux，也支持 Apple Silicon 上的 Docker Desktop。第一次先用测试工作区。

## 1. 下载配置

用普通用户执行，不用 root：

```sh
mkdir -p multica-bridge
cd multica-bridge
base=https://raw.githubusercontent.com/godlikejay/multica-agent-bridge/main
curl -fsSLo compose.yaml "$base/deploy/compose.yaml"
curl -fsSLo connections.json "$base/deploy/connections.example.json"
(umask 077; mkdir -p secrets data/runtime data/inbox data/outbox data/state)
printf 'BRIDGE_UID=%s\nBRIDGE_GID=%s\n' "$(id -u)" "$(id -g)" > .env
```

请在新目录中运行这些命令。示例默认连官方云；自建就把 `connections.json` 里的 `server_url` 改成容器能访问的 HTTPS API 基础地址，不要拼具体接口路径。多个服务或账号就在同一个文件里加条目，每条有不同的固定 ID 和令牌文件。令牌只给签发它的 Multica 服务用。

## 2. 保存令牌，启动

打开对应 Multica 网页的 **Settings → API Token**（[官方云入口](https://multica.ai/settings?tab=tokens)）创建令牌，完整值只显示一次。用 Bash 执行下面的命令；令牌不会出现在屏幕上，也不写进 JSON 和 `.env`：

```bash
(
  umask 077
  IFS= read -rsp 'Multica token: ' pat
  printf '\n'
  printf '%s\n' "$pat" > secrets/cloud.pat
)
```

如果 GHCR 包是私有的，先用有包读取权限的 GitHub 账号登录 GHCR；这跟上面的 Multica 令牌是两回事。

```sh
docker compose up -d
docker compose logs --tail 80 bridge
```

Compose 会按机器架构拉镜像，容器会拿挂载的令牌登录并启动 daemon。回到 Multica 网页，确认 `multica-bridge-cloud` 在线（末尾是连接 ID）。测试 Agent 绑定这个 runtime，选择 **Hermes**，分派一个测试 Issue。如果网页要求选本地目录，选容器内 `/bridge-runtime/cloud/workspaces` 下的空测试目录。看到 `data/outbox/cloud/` 下出现任务事件，就收到第一单了。[邮箱和 Agent 接入](../docs/zh-CN/agent-integration.md)

## 3. 回第一条消息

在安装目录用 Python 3 运行。工作区 ID 看 `scopes`，任务事件 ID 看 `list`，不是 Issue 编号。
```sh
base=https://raw.githubusercontent.com/godlikejay/multica-agent-bridge/main
curl -fsSLo mailbox.py "$base/skills/multica-bridge/scripts/mailbox.py"
python3 mailbox.py --root "$PWD/data" scopes
```

```sh
mailbox() { python3 mailbox.py --root "$PWD/data" --connection cloud --workspace WORKSPACE_UUID "$@"; }
mailbox list
mailbox read EVENT_ID
draft_dir=$(mktemp -d)
printf 'Bridge received this test Issue.\n' > "$draft_dir/reply.txt"
mailbox draft EVENT_ID --kind reply --body-file "$draft_dir/reply.txt" > "$draft_dir/reply.json"
mailbox submit "$draft_dir/reply.json"
```

在终端核对收件目标和回复内容再确认，然后回到 Multica 看原 Issue 是否出现回复。

## 连接配置

一条或多条连接都用同一份 `connections.json` 和 Compose。每条连接启动一个 daemon，对应一套服务和一个账号。同一工作区的不同项目不用分别启动 daemon。

| 字段 | 说明 |
| --- | --- |
| `id` | 固定且不重复的本地标识，用于 runtime 名称和邮箱路径。 |
| `server_url` | Docker 能访问的 HTTPS API 基础地址。 |
| `token_file` | 容器内 `/run/secrets/` 下的文件，如 `/run/secrets/cloud.pat`，默认对应宿主机的 `secrets/cloud.pat`。 |
| `workspace_ids` | 可选，仅限制 Bridge 接收哪些工作区的任务，不限制 daemon 在哪里注册。要限制注册范围，用只可访问目标工作区的账号。 |
| `max_active_runs` | 可选，每条连接的并发限制，须为正整数，也受 Multica 端限制。 |

每条连接使用自己的令牌文件。修改 `connections.json` 后，等正在跑的任务结束，再执行 `docker compose up -d --force-recreate bridge` 应用配置。分别到对应的 Multica 网页确认 runtime 在线，再分派测试 Issue。某条连接未上线时，可按连接 ID 查看 Compose 日志，也可在安装目录查询对应 daemon（两处 `cloud` 都换成对应的 ID）：

```sh
docker compose exec -T -e HOME=/bridge-runtime/cloud/home bridge \
  multica --profile cloud daemon status --output json
```

## 挂载与升级

默认配置是 `connections.json`，令牌在 `secrets/`，邮箱和运行状态在 `data/`。可以在 `.env` 用 `BRIDGE_CONFIG`、`BRIDGE_SECRETS_DIR`、`BRIDGE_DATA_DIR` 改位置；先在宿主机建好目录。不要给 Agent 访问令牌或 daemon 登录目录的权限。

确认没有正在跑的任务后，`docker compose pull && docker compose up -d` 更新镜像。想锁定版本可在 `.env` 用 `BRIDGE_IMAGE` 指定镜像 digest。保留 `data/runtime` 和 `data/state`。同一账号更换令牌时，更新文件，等正在跑的 Run 结束后执行 `docker compose restart bridge`。换账号或服务则用新的连接 ID，不搬运未处理的请求。互不信任的环境用各自的容器、目录和令牌。
