# 自建 CI Runner 基线

空库 MySQL、双 Runtime Worker、Worker 重启和 Chromium 全栈验收需要 Docker Compose。本目录提供厂商无关的 Linux Runner 镜像，可由 Jenkins、GitLab Runner、Woodpecker 或其他编排器调用。

构建 Runner：

```bash
docker build -f deploy/ci/runner/Dockerfile -t myclaw-ci-runner .
```

Runner 必须能够访问 Docker Engine，并以宿主机上的相同绝对路径挂载仓库。Docker Desktop 示例：

```bash
docker run -d \
  --name myclaw-ci-runner \
  --restart unless-stopped \
  --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD:$PWD" \
  -w "$PWD" \
  myclaw-ci-runner
```

Runner 容器通过 host network 访问 Compose 发布端口，并让 Chromium 继续使用安全上下文
`http://localhost`（前端依赖 `crypto.randomUUID()`）。Linux Docker Engine 必须保留上述
`--network host`；Docker Desktop 需要启用 host networking。若运行环境不支持 host network，
应把 Runner 直接安装在宿主机，而不是回退到非安全的 HTTP 自定义主机名。HTTPS 环境可通过
`MYCLAW_DOCKER_HOST_ADDR` 或完整的 `MYCLAW_FULLSTACK_API_URL`、`MYCLAW_FULLSTACK_UI_URL`
显式覆盖。

编排器只需设置固定 Commit SHA 后调用统一入口：

```bash
export MYCLAW_ACCEPTANCE_COMMIT_SHA="$(git rev-parse HEAD)"
test/scripts/run_ci_gates.sh standard
```

真实模型 Gate 必须是受保护的手工发布门禁，并通过编排器 Secret 注入 Key：

```bash
export MYCLAW_LLM_MODE=real
export MYCLAW_REAL_LLM_ENDPOINT='https://provider.example/v1'
export MYCLAW_REAL_LLM_MODEL='real-model-id'
test/scripts/run_ci_gates.sh real
```

`MYCLAW_REAL_LLM_API_KEY` 只能来自受保护的 Secret，禁止出现在仓库文件、命令回显、日志、Trace、测试快照或缓存中。失败诊断由统一入口脱敏后写入 `test/test-results/fullstack-failure.log`。
