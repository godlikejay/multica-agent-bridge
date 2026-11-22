# Multica Agent Bridge

[English](README.md) | 简体中文

Multica 派任务，Bridge 把任务写到本地，你自己的 Codex、agy 或 OMP 可以读任务、做任务、拟好回复。你确认提交后，Bridge 才把回复发回 Multica。它不会启动这些 Agent，也不会替你执行 Issue 里的命令。

## 安装

已有 Multica 服务和 Docker Compose 即可开始。镜像内置 Multica daemon 和 Bridge，不用自己安装 Go 或再装一套 Multica 服务。操作邮箱的脚本需要 Python 3。

1. [单连接安装](docs/zh-CN/deployment.md)：领令牌、填地址、启动容器、绑定 Agent，再收发一条测试消息。首次使用从这里开始。
2. [多连接安装](docs/zh-CN/multi-connection.md)：用同一个容器连接多套服务，或用多个账号连接同一套服务。
3. [接入你自己的 Agent](docs/zh-CN/agent-integration.md)：Codex、agy、OMP 共用一份 Skill。也可以不装 Skill，直接在终端操作。

[中文文档目录](docs/zh-CN/README.md) · [English documentation](docs/README.md)

先用测试工作区，不要把令牌、daemon 的登录目录或整个宿主机目录交给 Agent。Bridge 源码采用 [Apache-2.0](LICENSE)；镜像内的 Multica CLI/daemon 适用 [Multica 许可](https://github.com/multica-ai/multica/blob/v0.11.1/LICENSE)。
