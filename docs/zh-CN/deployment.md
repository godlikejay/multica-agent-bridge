# Docker 安装：从领令牌到回第一条消息

[English](../deployment.md) | 简体中文

这份指南适用于一个 Multica 服务、一个账号。先用有权限的测试工作区。你需要 Docker Compose v2；运行邮箱工具还需要 Python 3。镜像里已经有 daemon 和 Bridge。

## 1. 填地址，拉镜像

用普通用户在终端执行：

```sh
git clone https://github.com/godlikejay/multica-agent-bridge.git
cd multica-agent-bridge
[ -f .env ] || cp .env.example .env
id -u
id -g
(umask 077; mkdir -p .bridge/home .bridge/workspaces .bridge/inbox .bridge/outbox .bridge/state)
```

编辑 `.env`，用上面两条命令的数字分别填 `BRIDGE_UID` 和 `BRIDGE_GID`。使用官方云时，其他两项填：

```dotenv
MULTICA_SERVER_URL=https://api.multica.ai
BRIDGE_IMAGE=ghcr.io/godlikejay/multica-agent-bridge:latest
```

- `MULTICA_SERVER_URL` 是**容器要连的 API**：登录、领任务、发回复都走这里。自建用户向管理员要 HTTPS API 基础地址，不要加 `/api/me` 这样的接口路径。确认 Docker 容器能访问它。
- Multica 网页（官方云是 [multica.ai](https://multica.ai)）是你在浏览器里领令牌、绑定 Agent、查看 Issue 的地方，不用填进生产配置。旧配置里的 `MULTICA_APP_URL` 可以留着；它只是给上游浏览器流程的可选覆盖项。

```sh
docker compose pull bridge
```

如果镜像还未发布、没有读取私有包的权限，或你本来就想从源码构建，参见[英文安装指南里的镜像选项](../deployment.md#choose-an-image)。

## 2. 领令牌，启动 daemon

在浏览器登录你要连接的 Multica，打开 **Settings → API Token**（[官方云入口](https://multica.ai/settings?tab=tokens)），新建一个令牌。完整值只显示一次。它是 Multica 令牌，不是 GitHub 令牌。不写入 `.env`，也不发给 Agent。

在 Bash 中运行，提示时粘贴令牌（终端不会显示输入）：

```bash
(
  IFS= read -rsp 'Multica token: ' pat
  printf '\n'
  printf '%s\n' "$pat" | docker compose run --rm -T --no-deps bridge --profile bridge login --token
)
```

列出账号可用的工作区：

```sh
docker compose run --rm --no-deps bridge --profile bridge workspace list --full-id
```

用目标工作区的完整 ID 替换 `WORKSPACE_UUID`，不是工作区名称。然后启动：

```sh
docker compose run --rm --no-deps bridge --profile bridge config set workspace_id WORKSPACE_UUID
docker compose up -d --no-build bridge
docker compose exec bridge multica --profile bridge daemon status
```

登录留在 `.bridge/home`，按密钥目录保护，不要提交或共享。

## 3. 分派一单，回一条消息

回到 Multica，在目标工作区确认名为 `multica-agent-bridge` 的运行环境（runtime）在线。创建或编辑一个测试 Agent，绑定这个运行环境，选择 **Hermes**。这里的 Hermes 是 Bridge 对接 Multica 用的入口，不需要模型密钥。把一个处于 **Todo** 的测试 Issue 分派给这个 Agent。若页面要求选本地目录，用容器内 `/workspaces` 下的空目录，不要选真实项目。

在仓库根目录执行以下命令。将 `EVENT_ID` 换成 `list` 输出里的任务事件 ID，不是 Issue 编号。

```sh
export BRIDGE_ROOT="$PWD/.bridge"
tool=skills/multica-bridge/scripts/mailbox.py
python3 "$tool" list
python3 "$tool" read EVENT_ID
draft_dir=$(mktemp -d)
printf 'Bridge received this test Issue.\n' > "$draft_dir/reply.txt"
python3 "$tool" draft EVENT_ID --kind reply --body-file "$draft_dir/reply.txt" > "$draft_dir/reply.json"
python3 "$tool" submit "$draft_dir/reply.json"
```

核对终端里的收件目标和回复内容，再确认提交。**本地能看到任务、Multica 原 Issue 能看到回复**，才算两头都通。如果列表是空的，先查网页上的 runtime 是否在线、Issue 是否派给了正确的 Agent。跑通之后，再[接入你自己的 Agent](agent-integration.md)。
