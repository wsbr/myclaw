"""Make Runner fact projections idempotent without removing historical spans.

Revision ID: 0051
Revises: 0050
"""

import hashlib
import json
import re

from alembic import op
import sqlalchemy as sa


revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def _backfill_projection_keys(connection, *, batch_size: int = 500) -> None:
    """Claim one existing span per projection; keep every original span/link.

    Keyset batches avoid loading span payloads or all historical keys at once.
    Older duplicate rows deliberately stay NULL. A later replay finds the one
    canonical key and cannot append yet another copy. Existing row identifiers,
    attributes, timestamps and links are never rewritten or deleted.
    """
    span = sa.table(
        "observability_span", sa.column("id", sa.String(36)),
        sa.column("runner_trace_id", sa.String(36)),
        sa.column("name", sa.String(200)), sa.column("attributes", sa.JSON()),
        sa.column("projection_key", sa.String(64)),
    )
    fingerprint = span.c.attributes["fact_fingerprint"].as_string()
    last_id = ""
    while True:
        rows = connection.execute(sa.select(
            span.c.id, span.c.runner_trace_id, span.c.name,
            fingerprint.label("fingerprint"),
        ).where(
            span.c.id > last_id, span.c.projection_key.is_(None),
            fingerprint.is_not(None),
        ).order_by(span.c.id).limit(batch_size)).mappings().all()
        if not rows:
            return
        last_id = rows[-1]["id"]
        candidates = {}
        for row in rows:
            value = row["fingerprint"]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                continue
            # Frozen copy of the runtime encoding; migrations must not import
            # evolving application code or its database configuration.
            key = hashlib.sha256(json.dumps(
                [row["runner_trace_id"], row["name"], value],
                ensure_ascii=False, separators=(",", ":"),
            ).encode()).hexdigest()
            candidates.setdefault(key, row["id"])
        if not candidates:
            continue
        existing = set(connection.execute(sa.select(span.c.projection_key).where(
            span.c.projection_key.in_(candidates),
        )).scalars())
        updates = [
            {"span_row_id": row_id, "span_projection_key": key}
            for key, row_id in candidates.items() if key not in existing
        ]
        if updates:
            connection.execute(sa.update(span).where(
                span.c.id == sa.bindparam("span_row_id"),
            ).values(projection_key=sa.bindparam("span_projection_key")), updates)


def upgrade() -> None:
    op.add_column(
        "observability_span", sa.Column("projection_key", sa.String(64)),
    )
    op.create_unique_constraint(
        "uq_observability_span_projection", "observability_span", ["projection_key"],
    )
    _backfill_projection_keys(op.get_bind())


def downgrade() -> None:
    op.drop_constraint(
        "uq_observability_span_projection", "observability_span", type_="unique",
    )
    op.drop_column("observability_span", "projection_key")
