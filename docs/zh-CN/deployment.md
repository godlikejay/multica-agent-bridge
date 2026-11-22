# 在本地构建 Bridge 镜像

[English](../deployment.md) | 简体中文

日常使用按[现成镜像安装指南](../../deploy/README.zh-CN.md)操作。镜像已包含
Multica daemon 和 Bridge；只有测试运行镜像本身的改动时，才需要从源码构建。

在仓库目录运行：

```sh
make check
docker build -t multica-agent-bridge:local .
docker run --rm --entrypoint /usr/local/bin/multica-bridge multica-agent-bridge:local acp --version
```

验证本地镜像时，从同一个仓库检出版本复制模板到单独的测试目录：

```sh
test_dir=$(mktemp -d)
cp deploy/compose.yaml "$test_dir/compose.yaml"
cp deploy/connections.example.json "$test_dir/connections.json"
cd "$test_dir"
(umask 077; mkdir -p secrets data/runtime data/inbox data/outbox data/state)
printf 'BRIDGE_UID=%s\nBRIDGE_GID=%s\nBRIDGE_IMAGE=multica-agent-bridge:local\n' "$(id -u)" "$(id -g)" > .env
```

把 `connections.json` 指向测试服务，从[保存令牌，启动](../../deploy/README.zh-CN.md#2-保存令牌启动)
继续，并使用隔离的工作区。
集成测试另见[单服务](../../e2e/README.md)和
[双服务](../../e2e/multi/README.md)的测试说明。

[镜像构建流程](../../.github/workflows/container.yml)发布 Linux amd64 和 arm64 版本，
提供 `latest` 和 `sha-<完整提交哈希>` 标签。需要精确锁定时，使用已核验的镜像 digest。

只有 `main` 上的镜像构建输入或工作流改动会自动打包，也可以在 `main` 上手动触发。
