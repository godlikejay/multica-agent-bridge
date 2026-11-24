# Multica Agent Bridge

[English](README.md) | 简体中文

Multica 把已启动的任务交给 Bridge（MAB），Bridge 写进本地邮箱。你自己的 Codex、agy 或 OMP 可以读任务、发回复或结束一轮执行。Bridge 不会自行启动这些 Agent，也不会执行 Issue 里的指令。

## 安装

已有 Multica 服务和 Docker Compose 即可开始。镜像内置 Multica daemon 和 Bridge，不用自己安装 Go 或再装一套 Multica 服务。操作邮箱的脚本需要 Python 3。

- **用现成镜像（推荐）**：[免 clone 安装](deploy/README.zh-CN.md)。下载两个配置文件，保存 Multica 令牌，再运行 `docker compose up -d`。
- **自己构建镜像**：用仓库根目录的 [`Dockerfile`](Dockerfile)。拿到源码后，在仓库根目录执行 `docker build -t multica-agent-bridge:local .`，再在安装目录的 `.env` 中设置 `BRIDGE_IMAGE=multica-agent-bridge:local`。[完整构建步骤](docs/zh-CN/deployment.md)

两种方式都用同一份 Compose，可以配置一条或多条连接。

## 用短命令启停连接

镜像支持连接管理后，在 Compose 目录运行下面这行，就能在当前终端使用 `mab`：

```sh
mab() { docker compose exec -T bridge multica-bridge connection "$@"; }
```

可以用 `mab status`、`mab stop company`、`mab start company`。把 `company` 换成实际连接 ID。停止可能中断正在执行的 Run。想在任意目录使用，可以请 Docker 宿主机上的 Agent 按[一次性配置说明](deploy/README.zh-CN.md#shortcut-setup)帮你配好。

## 用自然语言操作 Multica

Bridge 上线后，[给 Codex、agy 或 OMP 安装 MAB Skill](docs/zh-CN/agent-integration.md)，然后在项目里打开 Agent，第一次告诉它：

> 帮这个项目配置 Multica。Bridge 邮箱在 `/实际安装路径/data`，用 `cloud` 连接，关注 `engineering` 和 `operations` 两个工作区。

`cloud` 换成你的连接 ID，工作区填网页地址里的简称（slug）。一个项目可以关注不同连接下的多个工作区。Agent 会把选择保存在项目的 `.local/mab.json`，并告诉你文件路径。不要把 Multica 令牌给 Agent。

之后不用再报路径或工作区，直接说：

- 「看下这个项目有哪些 Multica 任务。」
- 「读一下 OPS-123，回复说已收到，再说说你的初步看法。」
- 「OPS-123 有新留言吗？要不要调整方案？」
- 「接 OPS-123 的下一轮，需要时看一下附带的截图。」
- 「OPS-123 做完了，发个总结，结束这轮。」

同一会话连续接同一个 Issue 的 Run 时，Skill 可沿用上下文游标，避免重复读背景。
附件按需下载；需要在 Run 结束后保留的文件，请移出临时 Outbox。

这里看到的是 MAB **已收到**的任务，不包括 Multica 里尚未交付的排队任务。结束一轮执行不会关闭 Issue。手动配置或操作见[详细指南](docs/zh-CN/agent-integration.md)。

[中文文档目录](docs/zh-CN/README.md) · [English documentation](docs/README.md)

先用测试工作区，不要把令牌、daemon 的登录目录或整个宿主机目录交给 Agent。Bridge 源码采用 [Apache-2.0](LICENSE)；镜像内的 Multica CLI/daemon 适用 [Multica 许可](https://github.com/multica-ai/multica/blob/v0.11.1/LICENSE)。
