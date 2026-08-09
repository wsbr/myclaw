"""Bounded metadata-only Skill catalog for progressive disclosure."""

from __future__ import annotations

from collections.abc import Iterable


class SkillCatalogOverflow(ValueError):
    pass


def build_skill_catalog_sections(
    versions: Iterable[object], *, max_tokens: int,
) -> list[dict]:
    items = list(versions)
    if not items:
        return []
    max_chars = max_tokens * 4
    rows: list[dict] = []
    fixed_total = 0
    for version in items:
        skill_id = str(getattr(version, "skill_id"))
        checksum = str(getattr(version, "checksum"))
        path = f"skill://{skill_id}/{checksum}/SKILL.md"
        prefix = (
            "Available Skill (catalog metadata only; not the Skill instructions).\n"
            f"name: {getattr(version, 'manifest_name')}\n"
            "description: "
        )
        suffix = (
            f"\npath: {path}\n"
            "Copy this exact path into read_file before using the Skill; do not construct "
            "a skill URI from any other identifier. Then read only the referenced files needed."
        )
        fixed_total += len(prefix) + len(suffix)
        rows.append({
            "name": str(getattr(version, "manifest_name")),
            "description": str(getattr(version, "manifest_description")),
            "version_id": str(getattr(version, "id")),
            "checksum": checksum,
            "path": path,
            "prefix": prefix,
            "suffix": suffix,
        })
    if fixed_total > max_chars:
        raise SkillCatalogOverflow(
            f"Skill catalog metadata requires more than {max_tokens} tokens; reduce bound Skills"
        )
    description_budget = max_chars - fixed_total
    per_item = description_budget // len(rows)
    sections: list[dict] = []
    for row in rows:
        description = row["description"]
        if len(description) > per_item:
            description = description[:max(0, per_item - 1)] + "…"
        sections.append({
            "name": row["name"],
            "description": description,
            "version_id": row["version_id"],
            "checksum": row["checksum"],
            "path": row["path"],
            "content": row["prefix"] + description + row["suffix"],
        })
    return sections
