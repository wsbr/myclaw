# 自建 CI Runner 基线

第一阶段的空库 MySQL、双 Runtime Worker、Worker 重启和 Chromium 验收需要 Docker Compose。本目录提供厂商无关的 Linux Runner 镜像；后续可由自建 Jenkins、GitLab Runner、Woodpecker 或其他编排器调用，不依赖 GitHub Actions 或 Gitee Go。

构建 Runner：

```bash
docker build -f deploy/ci/runner/Dockerfile -t myclaw-ci-runner .
```

Runner 必须能够访问 Docker Engine，并以宿主机上的相同绝对路径挂载仓库。Docker Desktop 示例：

```bash
docker run -d \
  --name myclaw-ci-runner \
  --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD:$PWD" \
  -w "$PWD" \
  myclaw-ci-runner
```

编排器只需设置固定 Commit SHA 后调用统一入口：

```bash
export MYCLAW_ACCEPTANCE_COMMIT_SHA="$(git rev-parse HEAD)"
test/scripts/run_ci_gates.sh standard
```

真实模型 Gate 必须是受保护的手工发布门禁，并通过编排器 Secret 注入 Key：

```bash
export MYCLAW_LLM_MODE=real
export MYCLAW_REAL_LLM_ENDPOINT='https://dashscope.aliyuncs.com/compatible-mode/v1'
export MYCLAW_REAL_LLM_MODEL='qwen3.5-plus'
test/scripts/run_ci_gates.sh real
```

`MYCLAW_REAL_LLM_API_KEY` 只能来自受保护的 Secret，禁止出现在仓库文件、命令回显、日志、Trace、测试快照或缓存中。失败诊断由统一入口脱敏后写入 `test/test-results/fullstack-failure.log`。
