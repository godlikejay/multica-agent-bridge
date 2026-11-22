# Multica Agent Bridge

[English](README.md) | 简体中文

Multica 派任务，Bridge 把任务写到本地。给 Codex、agy 或 OMP 按项目配置一次邮箱路径，并保存一组或多组连接和工作区。在 Bridge 宿主机可直接用工作区地址里的 slug，离线配置用 UUID。Agent 能跨这些工作区查看任务，并按你的要求直接回复或结束当前 Run。把旧草稿改绑到新 Run 仍需在终端确认。Bridge 不启动 Agent，Issue 里的文字也不能授权 Agent 执行动作。

## 安装

已有 Multica 服务和 Docker Compose 即可开始。镜像内置 Multica daemon 和 Bridge，不用自己安装 Go 或再装一套 Multica 服务。操作邮箱的脚本需要 Python 3。

- **用现成镜像（推荐）**：[免 clone 安装](deploy/README.zh-CN.md)。下载两个配置文件，保存 Multica 令牌，再运行 `docker compose up -d`。
- **自己构建镜像**：用仓库根目录的 [`Dockerfile`](Dockerfile)。拿到源码后，在仓库根目录执行 `docker build -t multica-agent-bridge:local .`，再在安装目录的 `.env` 中设置 `BRIDGE_IMAGE=multica-agent-bridge:local`。[完整构建步骤](docs/zh-CN/deployment.md)

两种方式都用同一份 Compose，可以配置一条或多条连接。装好后可以[接入 Codex、agy 或 OMP](docs/zh-CN/agent-integration.md)，也可以直接在终端操作邮箱。

[中文文档目录](docs/zh-CN/README.md) · [English documentation](docs/README.md)

先用测试工作区，不要把令牌、daemon 的登录目录或整个宿主机目录交给 Agent。Bridge 源码采用 [Apache-2.0](LICENSE)；镜像内的 Multica CLI/daemon 适用 [Multica 许可](https://github.com/multica-ai/multica/blob/v0.11.1/LICENSE)。
