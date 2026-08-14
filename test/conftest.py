"""Root pytest configuration."""

import os
from pathlib import Path

# Load test env before any app imports
TEST_DIR = Path(__file__).resolve().parent
ENV_TEST = TEST_DIR / ".env.test"
if ENV_TEST.exists():
    for line in ENV_TEST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# The pre-v0.3 unit suite intentionally models one local installation and has
# many compact fixtures that construct aggregate rows directly. Preserve that
# meaning without weakening production models: direct legacy test rows are
# assigned the migration's default Tenant before SQLite flushes them. New
# Tenant-boundary tests always provide explicit Tenant IDs and therefore do not
# pass through this compatibility value.
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.agent_v1 import AgentBlueprint
from app.models.artifact_v1 import Artifact
from app.models.channel_v1 import ChannelAccount
from app.models.foundation import AgentApiKey, PlatformAccount, UserSession
from app.models.group_v1 import GroupSession
from app.models.memory_v1 import MemoryHead, MemoryRecord, MemoryWriteCandidate
from app.models.runtime_v1 import (
    AgentInvocation, DurableRunner, Run, ToolInvocation, WorkItem, Workspace,
)
from app.models.tenant_v1 import DEFAULT_TENANT_ID
from app.models.usage_v1 import UsageLedgerEntry


_TENANT_SCOPED_TEST_MODELS = (
    PlatformAccount, AgentApiKey, AgentBlueprint, UserSession, Run, WorkItem,
    DurableRunner, Workspace, ToolInvocation, AgentInvocation, GroupSession,
    MemoryHead, MemoryRecord, MemoryWriteCandidate, Artifact, ChannelAccount,
    UsageLedgerEntry,
)


@event.listens_for(Session, "before_flush")
def _backfill_legacy_single_tenant_test_rows(session, _flush_context, _instances):
    pending = list(session.new)
    for item in pending:
        if isinstance(item, _TENANT_SCOPED_TEST_MODELS) and item.tenant_id is None:
            item.tenant_id = DEFAULT_TENANT_ID

    accounts_by_user = {
        item.user_id: item for item in pending if isinstance(item, PlatformAccount)
    }
    sessions_by_id = {
        item.id: item for item in pending if isinstance(item, UserSession)
    }
    runs_by_id = {item.id: item for item in pending if isinstance(item, Run)}
    for item in pending:
        if isinstance(item, UserSession) and item.account_id is None:
            account = accounts_by_user.get(item.user_id)
            item.account_id = account.id if account is not None else item.user_id[:36]
    for item in pending:
        if isinstance(item, Run) and item.account_id is None:
            owner_session = sessions_by_id.get(item.session_id)
            item.account_id = (
                owner_session.account_id if owner_session is not None else item.user_id[:36]
            )
        elif isinstance(item, (ToolInvocation, AgentInvocation)) and item.account_id is None:
            owner_run = runs_by_id.get(item.run_id)
            item.account_id = (
                owner_run.account_id if owner_run is not None else "legacy-test-account"
            )
