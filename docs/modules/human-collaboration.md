# Agent-mediated human collaboration

This module defines how one Platform user can be contacted from another user's
Agent work without sharing the source Session or changing execution identity.

## Boundary

Human collaboration is independent from scheduling and Group mode:

- a Schedule decides when a Work directive is started;
- Group mode coordinates Agent identities;
- Clarification and Approval route a bounded action to a Platform account;
- the source Run, WorkItem and every replacement Runner continue to execute as
  the requesting account.

The Runtime never creates a Run as the recipient merely because another user
asked an Agent to contact them.

## Person resolution

`display_name` is mutable and non-unique, so it is presentation only.  The web
composer resolves `@handle` to an account inside the current Agent's contactable
member set and submits a structured mention.  The API validates the account and
stores an immutable `message_mention` snapshot.  The model receives an opaque,
HMAC-signed `principal_ref`, not identity authority from free-form text.

If a user writes a name without selecting an `@` mention, the model may call
`find_agent_user`.  The Tool searches only active accounts in the current
Tenant that the current Agent may contact.  A unique candidate can be used;
ambiguous results require the requesting user to choose.  The backend decodes
the reference and repeats the live Tenant, account and Agent-access checks when
the action is created.

For the current identity model, immutable `user_id` is projected as the handle.
A future Tenant-local handle may replace that projection without changing
stored account IDs or signed principal references.

## Cross-user Clarification

`ask_user` without `target_ref` retains its original behavior and asks the
source Session owner.  With a valid `target_ref`, it creates a Clarification
whose requester is the source account and whose target is the resolved account.

```text
Runner(A) -> Clarification(target=B) -> WorkItem waiting_user
B answer -> Clarification answered -> replacement Runner(A) queued
```

The recipient sees a RequiredAction card containing the requester, Agent,
question and explicitly supplied minimal context.  They do not receive read
access to A's Session.  The answer endpoint authorizes the target account
directly rather than requiring source-Session ownership.

The answer is recorded with B as the actor and sender while retaining A's
Session data scope.  It is injected into the replacement Runner as the result
of the original `ask_user` call.  It remains visible in A's timeline but is not
later replayed as though A authored a high-authority user instruction.

Private-Agent access is checked both when the request is created and when B
answers.  Revoking B's member grant therefore closes the path without exposing
the source Session.  Cancellation settles outstanding Clarifications through
the existing Run cancellation path.

## Approval boundary

Business approval and delegated permission remain distinct.  A future
cross-user Approval may reuse the same RequiredAction routing, but a permission
grant must be created only after a policy service proves that the target is an
eligible approver for an exact versioned subject.  Model-selected names cannot
mint authority.  Any delegated grant must bind grantor, beneficiary, resource,
action, expiry and use count; it must never expose the approver's credentials.

## Current scope

Implemented:

- Agent-scoped people directory and `@` mention UI;
- signed model-facing Platform-account references;
- `find_agent_user` discovery;
- cross-user `ask_user`, RequiredAction delivery and durable resume;
- source/target provenance and history authority separation.

Not yet implemented:

- cross-user business Approval;
- delegated permission grants;
- one-way result delivery and Artifact recipient grants;
- cross-Tenant recipients or external channels.
