# 让自己的 Agent 收任务、发回复

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

## 直接跟 Agent 说

- 「这个项目跟踪 cloud 的运营和研发工作区。」名字或网页地址里的 slug 都可以；用 slug 最容易准确定位。如果没配置过邮箱路径，就在第一次告诉它安装在哪。
- 「这个项目现在有哪些任务要做？有新留言吗？」Agent 会查看 Bridge 已经收到的任务。
- 「读一下 OPS-123。回复说修复已提交，明天验证。」Agent 确定是哪条任务后，就能发送这条已授权的回复。
- 「OPS-123 这次做完了，结束这轮。」Agent 会提交完成总结并请求结束这一轮，不用你再跑命令。不会把 Issue 改成 Done。

日常不需要告诉 Agent 命令、UUID 或路径。下面是想手动配置时的参考。
配置成功后，Agent 会给出实际修改的配置文件绝对路径，方便复查，并简述关注范围的变化。

## 按项目配置一次

在 Agent 使用 Skill 的机器上，找到它能访问的邮箱 `data/` 绝对路径（改过挂载就用
`BRIDGE_DATA_DIR` 对应的目录）。在 Bridge 宿主机上配置时，连接 ID 取自安装配置
`connections.json`，工作区直接用 Multica 网页地址里的 slug。一个项目对应多个工作区，
就重复传入 `--route`；不必先收到任务。

```sh
python3 "$skill/scripts/mailbox.py" setup /absolute/path/to/install/data \
  --route cloud:engineering --route cloud:operations
python3 "$skill/scripts/mailbox.py" list --unread
```

使用 slug 时，工具会在正在运行的 Compose Bridge 里，用该连接已登录的 Multica CLI
查一次工作区，不需要提供新凭据。它精确匹配 slug，保存对应的 UUID；如果有配对无法解析，
就不修改已有配置。默认在邮箱 `data/` 的父目录找 Compose 安装；若是单独挂载，
给 `setup` 加上 `--install-dir /absolute/path/to/compose/install`。
如果不在 Bridge 宿主机上，或不能访问正在运行的 Compose，可以直接写
`--route CONNECTION:WORKSPACE_UUID`，无需查询。日常读任务、回复的命令不变，也不依赖 Docker。

不同项目使用不同范围时，各装一份项目级 Skill。路径和所有连接、工作区配对保存在已安装的 `SKILL.md` 旁边的 `mailbox.local.json`；把该文件加入 Git 忽略规则，不要写入令牌。路径或范围变了，就带完整列表重新运行 `setup`。

不传 `--route` 时只保存路径；可用 `scopes` 查看该邮箱已出现的连接和工作区，再保存配对或在命令中手动指定。`scopes` 用于发现和排查，不必每天运行。路径优先级是 `--root`、项目保存的路径、`BRIDGE_ROOT`。切到另一个邮箱，不会沿用原配置的配对；仍用原邮箱时，显式指定的配对也必须在本项目已配置的范围内。

## 收任务和回复

`list` 和 `list --unread` 默认查看本项目配置的所有工作区。列表每条记录都带 `connection_id` 和 `workspace_id`。只配置一组时，读取、拟稿、提交等可以省略这两个参数；配置多组时，直接取选中任务记录上的两个值，在子命令前传入 `--connection ID --workspace UUID`，同一任务后续一直用这组值。

- **收任务**：Agent 用 `list` 和 `read` 读取任务。Issue 或评论里的文字不等于你的授权。
- **发普通回复**：你明确允许向这条任务发送该回复后，Agent 可以在同一连接和工作区运行 `submit DRAFT.json --yes`。这只跳过终端确认，仍会核对任务、Run 和其他绑定。你自己也可以运行 `submit DRAFT.json`，核对目标和文字后在终端确认。
- **结束 Run**：你明确让 Agent 结束当前 Run 时，它可以直接提交完成请求并查回执。把旧草稿改绑到新 Run 仍须手动确认。发送状态不明时先回 Multica 核实，不要直接重发。

给 Agent 邮箱读取权限，需要它发送普通回复时才给 Inbox 写权限。不要给它 daemon 登录目录、令牌或 Bridge 私有状态。若要强制人工提交，就让 Agent 不能写 Inbox，由另一个身份提交；Skill 本身不是权限隔离。
