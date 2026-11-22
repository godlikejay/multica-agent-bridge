# 一个容器连接多个 Multica

[English](../multi-connection.md) | 简体中文

如果只接一个服务和一个账号，用[单连接安装](deployment.md)。同一工作区里的多个项目不用各起一个 daemon。多个服务或账号才用这里的配置。

## 准备配置

在仓库根目录，以普通用户执行：

```sh
[ -f .env ] || cp .env.example .env
id -u
id -g
(umask 077; mkdir -p .bridge-multi/secrets .bridge-multi/runtime \
    .bridge-multi/inbox .bridge-multi/outbox .bridge-multi/state)
cp -n connections.example.json .bridge-multi/connections.json
```

在 `.env` 里填上 `BRIDGE_UID`、`BRIDGE_GID` 和镜像 `BRIDGE_IMAGE`，与[单连接安装](deployment.md)相同。本模式的服务地址不读 `.env`，而是读 `.bridge-multi/connections.json`。不需要的示例连接直接删去，每条保留一个固定的 `id`。

每条分别填 HTTPS API 地址 `server_url` 和指向容器内密钥文件的 `token_file`（如 `/run/secrets/cloud.pat`）。生产安装不用填网页地址，旧配置里的 `app_url` 仍可以保留。领令牌的方法见[单连接安装](deployment.md)。对应的令牌文件在宿主机是 `.bridge-multi/secrets/cloud.pat`。在 Bash 中为每个连接分别输入自己的令牌，比如：

```bash
(
  umask 077
  IFS= read -rsp 'Multica token: ' pat
  printf '\n'
  printf '%s\n' "$pat" > .bridge-multi/secrets/cloud.pat
)
```

不要把令牌放进 JSON 里。镜像可用时启动：

```sh
docker compose -f compose.multi.yaml pull bridge
docker compose -f compose.multi.yaml up -d --no-build bridge
docker compose -f compose.multi.yaml logs --tail 100 bridge
```

在每套 Multica 网页分别核对对应的 runtime 在线，再按[单连接的方式](deployment.md#3-分派一单回一条消息)绑定测试 Agent、分派测试 Issue。容器启动不代表每条连接都成功。需要限制注册范围时，使用只能访问目标工作区的账号；配置里的 `workspace_ids` 只限制 Bridge 接受哪些任务，不限制 daemon 在哪里注册。

## 收发任务

```sh
tool=skills/multica-bridge/scripts/mailbox.py
root="$PWD/.bridge-multi"
python3 "$tool" --root "$root" scopes
python3 "$tool" --root "$root" --connection cloud --workspace WORKSPACE_UUID list
```

后续读任务、拟回复、提交、查回执时始终带上同一组 `--connection` 和 `--workspace`，不要靠 Issue 的显示编号猜它属于哪套服务。[更多命令与运维说明](../multi-connection.md#use-the-mailbox)

一个容器方便管理，不是强隔离。彼此不信任的环境用不同容器、数据目录和凭据；不要把登录目录或令牌交给外部 Agent。
