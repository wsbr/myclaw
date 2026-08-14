"""Alembic must always expose one linear deployable head."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_alembic_has_one_head_and_unique_revision_ids():
    backend = Path(__file__).resolve().parents[3] / "backend"
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    revisions = list(ScriptDirectory.from_config(config).walk_revisions())

    revision_ids = [item.revision for item in revisions]
    assert len(revision_ids) == len(set(revision_ids))
    heads = [item.revision for item in revisions if item.is_head]
    assert heads == ["0037"]
