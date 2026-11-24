# 任务留在 Multica，什么时候在本地执行由你决定

[English](../workflow.md) | 简体中文

**在 Multica 派任务、看结果，在你熟悉的本地 Agent 里做事。**
Multica Agent Bridge（MAB）把两边接起来，你不用来回复制任务说明、截图和结果。

## 为什么要加一层 Bridge？

如果把编码工具直接接到 Multica Daemon，Multica 里的一次派单就可能让
Daemon 在所在机器上启动那个工具。这适合无人值守的工作。Multica 可以限制
谁能用这个运行环境，但工具启动后能接触什么，还取决于它的工作目录、沙箱和
可用凭证。[Multica 的运行环境如何工作](https://multica.ai/docs/daemon-runtimes)

**MAB 改变的是远程派单能启动什么。** Daemon 启动的是收发任务的邮箱进程，
不是你的编码 Agent。[标准部署](../../deploy/compose.yaml)把这个进程放在
容器里，不挂载你的代码仓库、主目录或 Docker socket。它把任务放进本地邮箱，
等结果回来。什么时候打开 Agent、在哪个项目里做、给它什么权限、要不要发回
结果，都由你决定。**Issue 可以传来任务，但不能替你授权执行命令或发回复。**

这层隔离不等于 Issue 内容可以盲信。它的前提是不给 Bridge 多余的访问权限，
本地 Agent 也仍按你的权限设置运行。

## 例子：手机上点「保存」没反应

1. **派任务。** 你在 Multica 建一个 Bug，附上私有截图，并给对接 Bridge 的
   Agent 发起一轮执行。你的本地编码 Agent 不会因此自动启动。
2. **在本地接任务。** 你在项目里打开自己的 Agent，说：「接下保存按钮的问题，
   需要的话看截图。」Agent 先读任务，需要截图才请 Bridge 下载。Bridge 会确认
   它属于这个 Issue，你不用把 Multica 令牌给本地 Agent。下载是临时的，想保留
   就另存一份。
3. **交回结果。** 看完修复后，你说：「把结果发回去，结束这轮。」团队能在原来
   的 Issue 里看到回复。到 Multica 确认这轮已结束。结束一轮不会把 Issue 关掉，
   也不妨碍继续跟进。
4. **跟进 Safari。** 你在同一个 Issue 发起下一轮：「再验证一下 iPhone Safari。」
   接着用同一个 Agent 会话，可以跳过已经读过且没变的背景；换新会话，也会拿到
   足够开始的 Issue 背景。两种方式都会收到这轮新的 Safari 要求。

[安装 MAB](../../deploy/README.zh-CN.md) · [配置本地 Agent](agent-integration.md)
