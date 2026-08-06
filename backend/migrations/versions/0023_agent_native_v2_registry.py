"""agent-native v2 immutable registry and Agent assembly snapshots

Revision ID: 0023
Revises: 0022

This is an expand/backfill migration. Existing UAT rows are preserved and
made reproducible; no historical Run or Artifact is rewritten.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    ]


def upgrade() -> None:
    # The old trigger does not know the newly added immutable columns. The
    # migration role replaces it around the one-time backfill.
    op.execute("DROP TRIGGER IF EXISTS trg_agent_version_sealed_update")
    op.execute("DROP TRIGGER IF EXISTS trg_agent_version_sealed_delete")
    for table in ("agent_version_tool", "agent_version_skill"):
        for operation in ("insert", "update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sealed_{operation}")

    op.add_column("model", sa.Column("name", sa.String(191), nullable=False, server_default=""))
    op.add_column("model", sa.Column("status", sa.String(20), nullable=False, server_default="active"))
    op.add_column("model", sa.Column("current_version_id", sa.String(36)))
    op.add_column("model", sa.Column("next_version_no", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("model", sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_model_status", "model", "status IN ('active','inactive')")

    op.create_table(
        "model_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("model_id", sa.String(36), sa.ForeignKey("model.id"), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(191), nullable=False),
        sa.Column("model_name", sa.String(191), nullable=False),
        sa.Column("endpoint", sa.String(500)),
        sa.Column("credential_ref", sa.String(500)),
        sa.Column("default_parameters", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("adapter_protocol", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("published_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id")),
        sa.UniqueConstraint("model_id", "version_no", name="uq_model_version_number"),
        sa.UniqueConstraint("model_id", "content_hash", name="uq_model_version_hash"),
    )
    op.create_index(
        "ix_model_version_model_published", "model_version", ["model_id", "published_at"]
    )
    op.create_foreign_key(
        "fk_model_current_version", "model", "model_version", ["current_version_id"], ["id"]
    )

    op.create_table(
        "conformance_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("suite_version", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("result_summary", sa.JSON()),
        sa.Column("evidence_artifact_id", sa.String(36)),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "subject_type IN ('model_version','platform_tool','channel_plugin','sandbox_profile_version')",
            name="ck_conformance_subject_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','passed','failed')",
            name="ck_conformance_status",
        ),
        sa.UniqueConstraint(
            "subject_type", "subject_id", "suite_version", "idempotency_key",
            name="uq_conformance_run_request",
        ),
    )
    op.create_index(
        "ix_conformance_subject_terminal", "conformance_run",
        ["subject_type", "subject_id", "suite_version", "status", "ended_at"],
    )

    op.add_column("platform_tool", sa.Column("side_effect", sa.String(20), nullable=False, server_default="none"))
    op.add_column("platform_tool", sa.Column("supports_cancel", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("platform_tool", sa.Column("supports_status_lookup", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.drop_constraint("ck_platform_tool_confirmation", "platform_tool", type_="check")
    op.create_check_constraint(
        "ck_platform_tool_confirmation", "platform_tool",
        "confirmation_policy IN ('never','always','per_call','session_grant')",
    )
    op.create_check_constraint(
        "ck_platform_tool_side_effect", "platform_tool",
        "side_effect IN ('none','idempotent','non_idempotent')",
    )

    op.add_column("skill_version", sa.Column("manifest", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")))
    op.add_column("skill_version", sa.Column("content_hash", sa.String(64), nullable=False, server_default=""))
    op.execute(sa.text("UPDATE skill_version SET content_hash = SHA2(CONCAT(checksum, ':', package_uri), 256) WHERE content_hash = ''"))
    op.drop_constraint("ck_skill_version_status", "skill_version", type_="check")
    op.create_check_constraint(
        "ck_skill_version_status", "skill_version",
        "status IN ('uploaded','validating','published','rejected','deprecated','quarantined')",
    )

    op.add_column("policy_definition", sa.Column("status", sa.String(20), nullable=False, server_default="active"))
    op.add_column("policy_definition", sa.Column("current_version_id", sa.String(36)))
    op.add_column("policy_definition", sa.Column("next_version_no", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("policy_definition", sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_policy_definition_status", "policy_definition", "status IN ('active','inactive')")
    op.create_table(
        "policy_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("policy_definition_id", sa.String(36), sa.ForeignKey("policy_definition.id"), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("implementation_ref", sa.String(500), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="published"),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint("status IN ('published','deprecated')", name="ck_policy_version_status"),
        sa.UniqueConstraint("policy_definition_id", "version_no", name="uq_policy_version_number"),
        sa.UniqueConstraint("policy_definition_id", "content_hash", name="uq_policy_version_hash"),
    )
    op.create_foreign_key(
        "fk_policy_current_version", "policy_definition", "policy_version",
        ["current_version_id"], ["id"],
    )

    op.add_column("sandbox_profile", sa.Column("status", sa.String(20), nullable=False, server_default="active"))
    op.add_column("sandbox_profile", sa.Column("current_version_id", sa.String(36)))
    op.add_column("sandbox_profile", sa.Column("next_version_no", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("sandbox_profile", sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"))
    op.create_table(
        "sandbox_profile_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sandbox_profile_id", sa.String(36), sa.ForeignKey("sandbox_profile.id"), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("provider_ref", sa.String(500), nullable=False),
        sa.Column("runtime_class", sa.String(191), nullable=False),
        sa.Column("image_digest", sa.String(191)),
        sa.Column("workspace_protocol_version", sa.String(64), nullable=False),
        sa.Column("artifact_protocol_version", sa.String(64), nullable=False),
        sa.Column("resource_policy", sa.JSON(), nullable=False),
        sa.Column("egress_policy", sa.JSON(), nullable=False),
        sa.Column("mount_policy", sa.JSON(), nullable=False),
        sa.Column("security_level", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="published"),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint("status IN ('published','deprecated')", name="ck_sandbox_version_status"),
        sa.CheckConstraint("security_level IN ('dev_directory','microvm')", name="ck_sandbox_version_security"),
        sa.UniqueConstraint("sandbox_profile_id", "version_no", name="uq_sandbox_version_number"),
        sa.UniqueConstraint("sandbox_profile_id", "content_hash", name="uq_sandbox_version_hash"),
    )
    op.create_foreign_key(
        "fk_sandbox_current_version", "sandbox_profile", "sandbox_profile_version",
        ["current_version_id"], ["id"],
    )

    # Freeze exact dependency pointers in AgentVersion.
    for name, target in (
        ("model_version_id", "model_version.id"),
        ("sandbox_profile_version_id", "sandbox_profile_version.id"),
        ("context_policy_version_id", "policy_version.id"),
        ("history_policy_version_id", "policy_version.id"),
        ("memory_write_policy_version_id", "policy_version.id"),
        ("memory_recall_policy_version_id", "policy_version.id"),
    ):
        del target
        op.add_column("agent_version", sa.Column(name, sa.String(36)))
    op.add_column("agent_version", sa.Column("discovery_config", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")))
    op.add_column("agent_version", sa.Column("collaboration_config", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")))
    op.add_column("agent_version", sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"))
    op.create_foreign_key("fk_agent_version_model_version", "agent_version", "model_version", ["model_version_id"], ["id"])
    op.create_foreign_key("fk_agent_version_sandbox_version", "agent_version", "sandbox_profile_version", ["sandbox_profile_version_id"], ["id"])
    for column in (
        "context_policy_version_id", "history_policy_version_id",
        "memory_write_policy_version_id", "memory_recall_policy_version_id",
    ):
        op.create_foreign_key(f"fk_agent_version_{column}", "agent_version", "policy_version", [column], ["id"])

    op.add_column("agent_version_tool", sa.Column("tool_registration_hash", sa.String(64)))
    op.add_column("agent_version_skill", sa.Column("skill_version_id", sa.String(36)))
    op.add_column("agent_version_skill", sa.Column("checksum", sa.String(64)))
    op.create_foreign_key(
        "fk_agent_version_skill_exact", "agent_version_skill", "skill_version",
        ["skill_version_id"], ["id"],
    )

    op.create_table(
        "agent_card",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id"), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.Column("name", sa.String(191), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("identity_kind", sa.String(64), nullable=False),
        sa.Column("domain_tags", sa.JSON(), nullable=False),
        sa.Column("supported_outcomes", sa.JSON(), nullable=False),
        sa.Column("accepted_request_summary", sa.Text(), nullable=False),
        sa.Column("capabilities_summary", sa.JSON(), nullable=False),
        sa.Column("collaboration_summary", sa.JSON(), nullable=False),
        sa.Column("declaration", sa.JSON(), nullable=False),
        sa.Column("attestation", sa.JSON(), nullable=False),
        sa.Column("availability", sa.String(20), nullable=False),
        sa.Column("group_enabled", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("agent_version_id", name="uq_agent_card_version"),
    )
    op.create_index(
        "ix_agent_card_discovery", "agent_card",
        ["visibility", "availability", "group_enabled"],
    )

    # Backfill exact versions for current mutable Registry rows. Model
    # conformance is intentionally not fabricated: publishing remains gated
    # until a v2 suite creates terminal passed evidence.
    op.execute(sa.text("""
        INSERT INTO model_version
          (id, model_id, version_no, provider, model_name, endpoint, credential_ref,
           default_parameters, capabilities, adapter_protocol, content_hash,
           published_at, published_by_account_id)
        SELECT UUID(), id, 1, provider, model_name, endpoint, credential_ref,
               default_parameters, capabilities, 'agent_native.v2',
               SHA2(CONCAT(provider, ':', model_name, ':', COALESCE(endpoint, ''), ':',
                     CAST(default_parameters AS CHAR), ':', CAST(capabilities AS CHAR)), 256),
               created_at, created_by_account_id
        FROM model
    """))
    op.execute(sa.text("""
        UPDATE model m JOIN model_version mv ON mv.model_id=m.id AND mv.version_no=1
        SET m.current_version_id=mv.id, m.next_version_no=2,
            m.name=CASE WHEN m.name='' THEN m.key ELSE m.name END
    """))
    op.execute(sa.text("""
        INSERT INTO policy_version
          (id, policy_definition_id, version_no, implementation_ref, parameters,
           content_hash, status, published_at, version)
        SELECT UUID(), id, 1, implementation_ref, default_parameters,
               SHA2(CONCAT(policy_type, ':', implementation_ref, ':', CAST(default_parameters AS CHAR)), 256),
               'published', created_at, 1
        FROM policy_definition
    """))
    op.execute(sa.text("""
        UPDATE policy_definition p JOIN policy_version pv
          ON pv.policy_definition_id=p.id AND pv.version_no=1
        SET p.current_version_id=pv.id, p.next_version_no=2
    """))
    op.execute(sa.text("""
        INSERT INTO sandbox_profile_version
          (id, sandbox_profile_id, version_no, provider_ref, runtime_class, image_digest,
           workspace_protocol_version, artifact_protocol_version, resource_policy,
           egress_policy, mount_policy, security_level, content_hash, status,
           published_at, version)
        SELECT UUID(), id, 1, implementation_ref, type, NULL, 'workspace.v1', 'artifact.v1',
               config, JSON_OBJECT(), JSON_OBJECT('private_agent', true, 'group_shared', true),
               'dev_directory', SHA2(CONCAT(type, ':', implementation_ref, ':', CAST(config AS CHAR)), 256),
               'published', created_at, 1
        FROM sandbox_profile
    """))
    op.execute(sa.text("""
        UPDATE sandbox_profile s JOIN sandbox_profile_version sv
          ON sv.sandbox_profile_id=s.id AND sv.version_no=1
        SET s.current_version_id=sv.id, s.next_version_no=2
    """))
    op.execute(sa.text("""
        UPDATE agent_version av JOIN model m ON m.id=av.model_id
        SET av.model_version_id=m.current_version_id
        WHERE av.publication_status IN ('published','deprecated','draft_test_snapshot')
    """))
    op.execute(sa.text("""
        UPDATE agent_version av JOIN sandbox_profile s ON s.id=av.sandbox_profile_id
        SET av.sandbox_profile_version_id=s.current_version_id
        WHERE av.publication_status IN ('published','deprecated','draft_test_snapshot')
    """))
    for logical, exact in (
        ("context_policy_id", "context_policy_version_id"),
        ("history_policy_id", "history_policy_version_id"),
        ("memory_write_policy_id", "memory_write_policy_version_id"),
        ("memory_recall_policy_id", "memory_recall_policy_version_id"),
    ):
        op.execute(sa.text(f"""
            UPDATE agent_version av JOIN policy_definition p ON p.id=av.{logical}
            SET av.{exact}=p.current_version_id
            WHERE av.publication_status IN ('published','deprecated','draft_test_snapshot')
        """))
    op.execute(sa.text("""
        UPDATE agent_version_tool avt JOIN platform_tool t ON t.id=avt.platform_tool_id
        SET avt.tool_registration_hash=t.registration_hash
    """))
    op.execute(sa.text("""
        UPDATE agent_version_skill avs JOIN skill s ON s.id=avs.skill_id
          JOIN skill_version sv ON sv.id=s.current_published_version_id
        SET avs.skill_version_id=sv.id, avs.checksum=sv.checksum
    """))
    # Historical published versions receive the same safe discovery projection
    # as newly published versions. Instructions and credentials are
    # intentionally absent; no Provider conformance evidence is fabricated.
    op.execute(sa.text("""
        INSERT INTO agent_card
          (id, agent_id, agent_version_id, visibility, name, description,
           identity_kind, domain_tags, supported_outcomes,
           accepted_request_summary, capabilities_summary,
           collaboration_summary, declaration, attestation, availability,
           group_enabled, content_hash, published_at)
        SELECT
          UUID(), av.agent_id, av.id, a.visibility, av.name, av.description,
          COALESCE(
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(av.discovery_config, '$.identity_kind')), 'null'),
            'agent'
          ),
          COALESCE(JSON_EXTRACT(av.discovery_config, '$.domain_tags'), JSON_ARRAY()),
          COALESCE(JSON_EXTRACT(av.discovery_config, '$.supported_outcomes'), JSON_ARRAY()),
          COALESCE(
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(av.discovery_config, '$.accepted_request_summary')), 'null'),
            ''
          ),
          JSON_OBJECT(
            'model_version_id', av.model_version_id,
            'sandbox_profile_version_id', av.sandbox_profile_version_id,
            'tool_keys', COALESCE((
              SELECT JSON_ARRAYAGG(t.tool_key)
              FROM agent_version_tool avt JOIN platform_tool t ON t.id=avt.platform_tool_id
              WHERE avt.agent_version_id=av.id
            ), JSON_ARRAY()),
            'skill_version_ids', COALESCE((
              SELECT JSON_ARRAYAGG(avs.skill_version_id)
              FROM agent_version_skill avs WHERE avs.agent_version_id=av.id
            ), JSON_ARRAY())
          ),
          av.collaboration_config,
          JSON_OBJECT(
            'description', av.description,
            'domain_tags', COALESCE(JSON_EXTRACT(av.discovery_config, '$.domain_tags'), JSON_ARRAY()),
            'supported_outcomes', COALESCE(JSON_EXTRACT(av.discovery_config, '$.supported_outcomes'), JSON_ARRAY()),
            'accepted_request_summary', COALESCE(
              NULLIF(JSON_UNQUOTE(JSON_EXTRACT(av.discovery_config, '$.accepted_request_summary')), 'null'),
              ''
            )
          ),
          JSON_OBJECT(
            'assembly_content_hash', av.content_hash,
            'conformance_suite_version', 'agent-native-v2.1',
            'model_version_id', av.model_version_id,
            'backfilled_by_migration', '0023'
          ),
          CASE WHEN a.status='published' THEN 'available' ELSE 'unavailable' END,
          av.group_enabled,
          SHA2(CONCAT(av.id, ':', COALESCE(av.content_hash, ''), ':', a.visibility), 256),
          COALESCE(av.published_at, av.immutable_at, av.created_at)
        FROM agent_version av JOIN agent a ON a.id=av.agent_id
        WHERE av.publication_status IN ('published','deprecated')
    """))

    for table in ("model_version", "agent_card"):
        op.execute(sa.text(
            f"CREATE TRIGGER trg_{table}_immutable_update BEFORE UPDATE ON {table} FOR EACH ROW "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is immutable'"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_{table}_immutable_delete BEFORE DELETE ON {table} FOR EACH ROW "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is immutable'"
        ))
    op.execute(sa.text("""
        CREATE TRIGGER trg_conformance_run_terminal_update
        BEFORE UPDATE ON conformance_run FOR EACH ROW
        BEGIN
          IF OLD.status IN ('passed','failed') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'terminal conformance evidence is immutable';
          END IF;
        END
    """))
    op.execute(sa.text(
        "CREATE TRIGGER trg_conformance_run_immutable_delete BEFORE DELETE ON conformance_run "
        "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'conformance evidence is append-only'"
    ))
    _create_v2_agent_triggers()


def _create_v2_agent_triggers() -> None:
    op.execute("""
        CREATE TRIGGER trg_agent_version_sealed_update
        BEFORE UPDATE ON agent_version FOR EACH ROW
        BEGIN
          IF OLD.immutable_at IS NOT NULL AND NOT (
            OLD.publication_status = 'published' AND NEW.publication_status = 'deprecated'
            AND OLD.id <=> NEW.id AND OLD.agent_id <=> NEW.agent_id
            AND OLD.version_no <=> NEW.version_no AND OLD.draft_revision <=> NEW.draft_revision
            AND OLD.derived_from_version_id <=> NEW.derived_from_version_id
            AND OLD.name <=> NEW.name AND OLD.description <=> NEW.description
            AND OLD.instructions <=> NEW.instructions AND OLD.model_id <=> NEW.model_id
            AND OLD.model_version_id <=> NEW.model_version_id
            AND OLD.sandbox_profile_id <=> NEW.sandbox_profile_id
            AND OLD.sandbox_profile_version_id <=> NEW.sandbox_profile_version_id
            AND OLD.context_policy_id <=> NEW.context_policy_id
            AND OLD.context_policy_version_id <=> NEW.context_policy_version_id
            AND OLD.history_policy_id <=> NEW.history_policy_id
            AND OLD.history_policy_version_id <=> NEW.history_policy_version_id
            AND OLD.memory_write_policy_id <=> NEW.memory_write_policy_id
            AND OLD.memory_write_policy_version_id <=> NEW.memory_write_policy_version_id
            AND OLD.memory_recall_policy_id <=> NEW.memory_recall_policy_id
            AND OLD.memory_recall_policy_version_id <=> NEW.memory_recall_policy_version_id
            AND OLD.group_enabled <=> NEW.group_enabled
            AND OLD.discovery_config <=> NEW.discovery_config
            AND OLD.collaboration_config <=> NEW.collaboration_config
            AND OLD.created_by_account_id <=> NEW.created_by_account_id
            AND OLD.published_at <=> NEW.published_at AND OLD.content_hash <=> NEW.content_hash
            AND OLD.immutable_at <=> NEW.immutable_at
          ) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'sealed agent version is immutable';
          END IF;
        END
    """)
    op.execute("""
        CREATE TRIGGER trg_agent_version_sealed_delete
        BEFORE DELETE ON agent_version FOR EACH ROW
        BEGIN
          IF OLD.immutable_at IS NOT NULL THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'sealed agent version cannot be deleted';
          END IF;
        END
    """)
    for table in ("agent_version_tool", "agent_version_skill"):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            reference = "NEW" if operation != "DELETE" else "OLD"
            op.execute(f"""
                CREATE TRIGGER trg_{table}_sealed_{operation.lower()}
                BEFORE {operation} ON {table} FOR EACH ROW
                BEGIN
                  IF EXISTS (
                    SELECT 1 FROM agent_version
                    WHERE id = {reference}.agent_version_id AND immutable_at IS NOT NULL
                  ) THEN
                    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'sealed agent assembly is immutable';
                  END IF;
                END
            """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_conformance_run_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_conformance_run_terminal_update")
    for table in ("agent_card", "model_version"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_update")
    for table in ("agent_version_tool", "agent_version_skill"):
        for operation in ("insert", "update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sealed_{operation}")
    op.execute("DROP TRIGGER IF EXISTS trg_agent_version_sealed_update")
    op.execute("DROP TRIGGER IF EXISTS trg_agent_version_sealed_delete")
    op.drop_table("agent_card")
    op.drop_constraint("fk_agent_version_skill_exact", "agent_version_skill", type_="foreignkey")
    op.drop_column("agent_version_skill", "checksum")
    op.drop_column("agent_version_skill", "skill_version_id")
    op.drop_column("agent_version_tool", "tool_registration_hash")
    for column in (
        "memory_recall_policy_version_id", "memory_write_policy_version_id",
        "history_policy_version_id", "context_policy_version_id",
    ):
        op.drop_constraint(f"fk_agent_version_{column}", "agent_version", type_="foreignkey")
    op.drop_constraint("fk_agent_version_sandbox_version", "agent_version", type_="foreignkey")
    op.drop_constraint("fk_agent_version_model_version", "agent_version", type_="foreignkey")
    for column in (
        "version", "collaboration_config", "discovery_config",
        "memory_recall_policy_version_id", "memory_write_policy_version_id",
        "history_policy_version_id", "context_policy_version_id",
        "sandbox_profile_version_id", "model_version_id",
    ):
        op.drop_column("agent_version", column)
    op.drop_constraint("fk_sandbox_current_version", "sandbox_profile", type_="foreignkey")
    op.drop_table("sandbox_profile_version")
    for column in ("version", "next_version_no", "current_version_id", "status"):
        op.drop_column("sandbox_profile", column)
    op.drop_constraint("fk_policy_current_version", "policy_definition", type_="foreignkey")
    op.drop_table("policy_version")
    op.drop_constraint("ck_policy_definition_status", "policy_definition", type_="check")
    for column in ("version", "next_version_no", "current_version_id", "status"):
        op.drop_column("policy_definition", column)
    op.drop_constraint("ck_skill_version_status", "skill_version", type_="check")
    op.create_check_constraint(
        "ck_skill_version_status", "skill_version",
        "status IN ('uploaded','validating','published','rejected','deprecated')",
    )
    op.drop_column("skill_version", "content_hash")
    op.drop_column("skill_version", "manifest")
    op.drop_constraint("ck_platform_tool_side_effect", "platform_tool", type_="check")
    op.drop_constraint("ck_platform_tool_confirmation", "platform_tool", type_="check")
    op.create_check_constraint(
        "ck_platform_tool_confirmation", "platform_tool",
        "confirmation_policy IN ('never','always')",
    )
    op.drop_column("platform_tool", "supports_status_lookup")
    op.drop_column("platform_tool", "supports_cancel")
    op.drop_column("platform_tool", "side_effect")
    op.drop_table("conformance_run")
    op.drop_constraint("fk_model_current_version", "model", type_="foreignkey")
    op.drop_table("model_version")
    op.drop_constraint("ck_model_status", "model", type_="check")
    for column in ("version", "next_version_no", "current_version_id", "status", "name"):
        op.drop_column("model", column)
