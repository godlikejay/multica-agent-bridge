# 在本地构建 Bridge 镜像

[English](../deployment.md) | 简体中文

日常使用按[现成镜像安装指南](../../deploy/README.zh-CN.md)操作。镜像已包含
Multica daemon 和 Bridge。如果想自行构建或测试镜像改动，使用仓库根目录的 [`Dockerfile`](../../Dockerfile)。

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
要验证真实 Multica 接口，见 [Docker 冒烟测试](../../e2e/README.md)。

[镜像构建流程](../../.github/workflows/container.yml)发布 Linux amd64 和 arm64 版本，
提供 `latest` 和 `sha-<完整提交哈希>` 标签。需要精确锁定时，使用已核验的镜像 digest。

[Regression 工作流](../../.github/workflows/regression.yml)仅在 `main` 上同一次提交的三项回归全部通过后调用镜像发布。镜像构建输入或发布工作流改动才自动打包，纯测试改动不打包。要手动重打，请在 `main` 上运行 **Regression**。
