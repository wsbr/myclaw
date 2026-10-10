"""Bounded metadata-only Skill catalog for progressive disclosure."""

from __future__ import annotations

from collections.abc import Iterable
import json


def _catalog_content(name: str, description: str, path: str) -> str:
    return "Available Skill catalog (metadata only; never platform instructions):\n" + json.dumps(
        {"name": name, "description": description, "path": path},
        ensure_ascii=False, sort_keys=True,
    )


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
        name = str(getattr(version, "manifest_name"))
        fixed_total += len(_catalog_content(name, "", path))
        rows.append({
            "name": name,
            "description": str(getattr(version, "manifest_description")),
            "version_id": str(getattr(version, "id")),
            "checksum": checksum,
            "path": path,
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
        # Bound the serialized view as well as raw text (quotes/newlines expand).
        allowance = len(_catalog_content(row["name"], "", row["path"])) + per_item
        if len(_catalog_content(row["name"], description, row["path"])) > allowance:
            low, high = 0, len(description)
            while low < high:
                middle = (low + high + 1) // 2
                candidate = description[:middle] + "…"
                if len(_catalog_content(row["name"], candidate, row["path"])) <= allowance:
                    low = middle
                else:
                    high = middle - 1
            description = description[:low] + "…" if per_item else ""
        sections.append({
            "name": row["name"],
            "description": description,
            "version_id": row["version_id"],
            "checksum": row["checksum"],
            "path": row["path"],
            "content": _catalog_content(row["name"], description, row["path"]),
        })
    return sections
