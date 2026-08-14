# Emergent Group demo: Recovery → Evidence → Security

This demonstration shows MyClaw's intended Group semantics without a Group template, role slots, a fixed collaboration graph, or a permanent supervisor.

## Prerequisites

1. Start MyClaw with `make up` and sign in as the bootstrap administrator.
2. Configure and activate a Provider ModelVersion that passes native Tool-call conformance.
3. Ensure the Runtime and Sandbox workers are healthy.

## Create three Agents

Use the following names and public descriptions. Instructions may elaborate professional behavior, but must not encode a fixed handoff graph.

### Recovery

```text
Name: Recovery
Description: Owns incident recovery planning, restoration sequencing, operational risk tracking, and user-facing recovery communication.
```

Recovery is the initial Chat Agent and conversation anchor. Enable Group participation and allow `invoke_agent`, Group coordination, read-only evidence Tools, and the Tools required by the demonstration environment.

### Evidence

```text
Name: Evidence
Description: Reviews incident records and recovery claims, builds an evidence matrix, identifies unsupported conclusions, and requests specialist verification when needed.
```

Enable Group participation and the same coordination/history capabilities. Evidence should have enough read access to inspect the demonstration material.

### Security

```text
Name: Security
Description: Verifies identity, authorization, secret handling, isolation, auditability, and security consequences of proposed recovery actions.
```

Enable Group participation and the read-only security evidence capabilities needed by the scenario.

Publish all three AgentVersions. Their AgentCards must be visible and callable by the bootstrap account.

## Start from normal Chat

Open a normal Chat with Recovery and send:

```text
我们准备恢复一个发生过凭证泄露和证据缺口的服务。请审查现有恢复方案，确认关键事实是否有证据支持，并检查恢复动作是否会重新引入安全风险。最后给我一份可以执行的结论，明确未解决的风险和分歧。
```

Do not tell Recovery which Agent to call. The governed candidate catalog contains all currently eligible Group-enabled Agent names/descriptions; the model should decide whether peers are materially useful.

## Expected semantics

The exact wording and number of model Turns are not fixed. The following machine-visible invariants are expected:

1. The normal Chat answer commits before Group emergence begins.
2. The model selects the smallest useful member set from governed candidates; it does not select every Agent by default.
3. Recovery remains the conversation anchor.
4. Recovery performs one primary asynchronous handoff instead of waiting synchronously for every member.
5. The primary owner decides semantically whether another specialist is needed.
6. Every handoff creates a directed human-visible `@ request` and an `AgentInvocation`.
7. Every completed delegated task creates one directed human-visible `@ delivery` to its caller.
8. The same delivery is not copied into an additional Group broadcast.
9. Group-visible progress notes wake nobody and are not automatically injected into other members' prompts.
10. The final Reporter summary is emitted only after durable Group work becomes quiescent.

A healthy interaction may look like:

```text
Recovery --request--> Evidence
Evidence --request--> Security
Security --delivery--> Evidence
Evidence --delivery--> Recovery
Recovery --summary--> User
```

This is an observed interaction graph, not a graph configured in advance.

## Inspect the evidence

Use the Group panel and Run trace views to verify:

- frozen GroupRun members and AgentVersion IDs;
- each AgentInvocation caller, target, status, request, and result;
- directed request/delivery Message IDs;
- Runner and AgentTurn sequence;
- ProviderAttempt and ToolInvocation facts;
- absence of duplicate Group delivery messages;
- final Group quiescence and Reporter summary event.

The authenticated APIs also expose:

```text
GET /api/v1/runs/{run_id}
GET /api/v1/runs/{run_id}/events
GET /api/v1/runs/{run_id}/runners
GET /api/v1/runners/{runner_id}/trace
```

## Troubleshooting

- **No Group emerged:** confirm all peer AgentVersions are published, visible, callable, Group-enabled, and included in the complete governed candidate snapshot.
- **Every Agent was selected:** improve AgentCard descriptions and inspect the selection prompt/trace; do not add a fixed role router.
- **Recovery waited for a result:** confirm Group `invoke_agent` uses asynchronous handoff semantics and the current Runtime release.
- **Duplicate delivery:** compare `AgentInvocation.result_message_id`, directed delivery Message, and any Group-visible note; automatic delivery must exist exactly once.
- **A member did not see another member's work:** directed messages wake only their recipient. Other members must deliberately use Group history search when that history is relevant.
