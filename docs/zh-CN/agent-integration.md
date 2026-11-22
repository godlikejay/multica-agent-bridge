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
python3 "$skill/scripts/mailbox.py" tasks
```

使用 slug 时，工具会在正在运行的 Compose Bridge 里，用该连接已登录的 Multica CLI
查一次工作区，不需要提供新凭据。它精确匹配 slug，保存对应的 UUID；如果有配对无法解析，
就不修改已有配置。默认在邮箱 `data/` 的父目录找 Compose 安装；若是单独挂载，
给 `setup` 加上 `--install-dir /absolute/path/to/compose/install`。
如果不在 Bridge 宿主机上，或不能访问正在运行的 Compose，可以直接写
`--route CONNECTION:WORKSPACE_UUID`，无需查询。日常读任务、回复不依赖 Docker。

从项目里运行 `setup`。它会在最近的 Git 工作树根目录保存 `.local/mab.json`；不在 Git 工作树里时，就保存到当前目录。配置按项目区分，多个项目可以共用同一份 Skill，包括软链接安装。把 `/.local/mab.json` 加入项目的 Git 忽略规则，不要写入令牌。邮箱路径或范围变了，带完整列表重新运行 `setup`。如果从其他目录操作，在子命令前加 `--project /absolute/path/to/project`。

不传 `--route` 时只保存路径；可用 `scopes` 查看该邮箱已出现的连接和工作区，再保存配对或在命令中手动指定。`scopes` 用于发现和排查，不必每天运行。路径优先级是 `--root`、项目保存的路径、`BRIDGE_ROOT`。切到另一个邮箱，不会沿用原配置的配对；仍用原邮箱时，显式指定的配对也必须在本项目已配置的范围内。

## 收任务和回复

日常直接说「看下 Multica 任务」「读一下 OPS-123 的最新要求」即可。Agent 按项目保存的范围查找，无需你重复填工作区 ID；同一个任务编号在多个工作区重名时，不会擅自选一个。

如果想自己在终端操作：

```sh
tool=.agents/skills/multica-bridge/scripts/mailbox.py
python3 "$tool" tasks
python3 "$tool" tasks OPS-123
python3 "$tool" show OPS-123
# 写操作使用 show 返回的确切 target，不用任务编号。
python3 "$tool" reply TARGET '修复已提交，明天验证。'
# 如果要结束这一轮，用 complete 代替 reply。
python3 "$tool" complete TARGET '修复已完成，检查通过。'
# 仅当回执尚未返回时，用命令返回的请求 ID 查询。
python3 "$tool" result REQUEST_ID
```

`tasks` 每个 Issue 只展示 MAB 最近收到的 Run，不是 Multica 的完整队列，也不代表 Run 仍在执行。`show` 查看一条任务或指定事件；回复某条评论时，使用该评论的 target。新 Run 交给 MAB 后才会出现在本地，旧 Run 的回复或草稿不会自动改绑。

`reply` 和 `complete` 会直接提交，只有明确授权才使用。两者都会校验对应事件和 Run；要先拟稿或在终端核对，仍可用 `draft`、`submit`。旧草稿改绑必须手动确认。发送状态不明时查原 Issue 或等待核对，不要直接重发。收到完成请求的回执不代表远端已结束；结束 Run 也不会关闭 Issue。

只给 Agent 必要的邮箱读取权限，需要它提交已授权动作时才给 Inbox 写权限。不要给它 daemon 登录目录、令牌或 Bridge 私有状态。若要强制人工提交，就让 Agent 不能写 Inbox，由另一个身份提交；Skill 本身不是权限隔离。
