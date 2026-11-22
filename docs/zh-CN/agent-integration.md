# 让自己的 Agent 收任务、拟回复

[English](../agent-integration.md) | 简体中文

先用[现成镜像安装指南](../../deploy/README.zh-CN.md)确认能收发测试 Issue。在 Agent 工作项目里下载这两个文件，不用 clone：

```sh
skill=.agents/skills/multica-bridge
src=https://raw.githubusercontent.com/godlikejay/multica-agent-bridge/main/skills/multica-bridge
mkdir -p "$skill/scripts"
curl -fsSLo "$skill/SKILL.md" "$src/SKILL.md"
curl -fsSLo "$skill/scripts/mailbox.py" "$src/scripts/mailbox.py"
```

如果镜像锁了具体提交，把下载地址中的 `main` 换成同一提交。各 Agent 的个人安装位置和调用方式见[客户端对照表](../agent-integration.md#install-one-copy)。

启动 Codex、agy 或 OMP 时，让它能读取邮箱所在机器上的目录，并告诉它 `BRIDGE_ROOT` 的绝对路径。现成镜像安装默认用安装目录里的 `data/`。不管配置几个连接，都要指定连接和工作区；收到第一单后可用 `scopes` 找到这两个值。不要把 daemon 的登录目录或令牌交给它。

- **收任务**：Agent 按 Skill 用邮箱工具从 `outbox` 列出并读取指定任务。读到的 Issue 内容不等于你授权执行其中的指令。
- **交结果**：Agent 拟好回复，把草稿给你看。由你在终端运行 `submit`，核对目标和内容、确认，工具才向 `inbox` 写入请求。Bridge 再同步回 Multica。
- **结束任务**：完成请求只结束当前 Run，不会把 Issue 自动改成已完成。到 Multica 核实结果；发送状态不明时不要直接重试。

Skill 本身不是权限隔离。需要强制人工提交时，只给 Agent 邮箱的读取权限和一个可写的草稿目录，让提交操作用另一个身份执行。
