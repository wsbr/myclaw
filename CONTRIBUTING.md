# Contributing to MyClaw

Thank you for helping improve MyClaw.

## Before opening a change

MyClaw follows a few architectural invariants:

- Chat、Plan和Group共享一个Agent Loop。
- WorkingPlan是模型维护的可见状态，不是平台执行DAG。
- Group成员由受治理Agent目录语义选择，不使用固定角色槽位或永久Supervisor。
- Tool、Agent调用和副作用必须经过机器权限、持久Invocation和可审计结果。
- 普通Assistant消息可以自然结束Turn；文本声明不能替代真实Tool、Artifact或Verification。
- Agent是逻辑身份，不绑定常驻进程或网络地址。

Large semantic changes should update the relevant authoritative document under `docs/modules/` in the same pull request.

## Development setup

```bash
make bootstrap
make up
make test-env
make test
```

The core UI is available at <http://localhost:8080>; API documentation is at <http://localhost:8000/docs>.

## Required checks

Before opening a pull request:

```bash
make release-check
```

At minimum, changes must pass:

- repository secret and sensitive-file checks;
- `git diff --check`;
- all deterministic backend unit tests;
- the frontend TypeScript production build;
- Docker Compose configuration validation.

Real-model or external-Provider changes should include a sanitized acceptance summary or a link to an access-controlled CI artifact. Do not commit raw screenshots, transcripts, traces, reports, environment metadata, or real credentials.

## Pull requests

- Keep one coherent change per pull request.
- Explain the user-visible behavior and state-machine impact.
- Add regression tests for bugs and durable-state changes.
- Preserve existing migrations; add a new migration for schema changes.
- Do not rewrite historical acceptance evidence to describe new behavior.
- Do not commit generated UAT evidence, backups, workspaces, `.env`, or build output.

## Commit messages

Concise conventional prefixes are encouraged:

```text
feat: add bounded provider admission
fix: prevent duplicate group delivery
docs: clarify group message visibility
test: cover runner lease recovery
```
