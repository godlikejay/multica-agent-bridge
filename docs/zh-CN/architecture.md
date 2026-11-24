# 架构与维护入口

[English](../architecture.md) | 简体中文

Multica 负责 Issue、分派和 Run 调度。Bridge 把分派到的 Run 转成本地信箱文件；操作者决定使用哪个本地编码 Agent、在哪个项目里运行以及授予什么权限。Bridge 不启动编码 Agent，Agent 也不需要 Multica 令牌。

1. [安装用 Compose](../../deploy/compose.yaml) 启动 `multica-bridge serve`。[Supervisor](../../internal/supervisor) 为每个启用的连接管理一个 daemon。仓库根目录的 Compose 文件只供开发，[e2e/compose.yaml](../../e2e/compose.yaml) 则是独立的临时测试环境。
2. Multica daemon 在收到 Run 时启动 `multica-bridge acp`，而不是通用编码环境。[入口](../../cmd/multica-bridge/main.go)和 [ACP 适配器](../../internal/acp)负责握手和取消；[Multica 客户端](../../internal/multica)核实任务身份并处理评论、附件。
3. [Run worker](../../internal/bridge)按连接、工作区、Issue、Run 和 epoch（本地代次）绑定任务；[文件信箱](../../internal/mailbox)负责目录隔离与本地持久化。不同 Issue 可以并行，同一 Issue 的任务串行。如果无法确认回复是否已发出，不会盲目重发。
4. 操作者通过 [Skill 和 Python 信箱工具](../../skills/multica-bridge/SKILL.md)操作文件。接单和标记已读只影响本地；授权回复不结束 Run，授权完成只请求结束对应 Run，不改变 Issue 状态。最终是否完成要到 Multica 查看，本地选中的 Run 不一定仍在运行。

默认安装只挂载配置、凭据、运行数据和信箱，不挂载宿主机的仓库、家目录或 Docker socket。远程 Issue 和附件的内容都是不可信数据，不授予本地执行、回复或结束 Run 的权限。

修改信箱字段、附件或消息投递，请先读[文件协议](../protocol.md)，核对 Go 写入端和 Python 读取端。修改日常命令，请从 [Skill](../../skills/multica-bridge/SKILL.md)和[信箱工具测试](../../skills/multica-bridge/scripts)入手。修改上游接口或身份校验时，按[当前固定版本](../decisions.md)核查源码。镜像改动见[本地构建](deployment.md)；各层测试能验证什么、何时发布镜像，见[贡献指南](../../CONTRIBUTING.md)。
