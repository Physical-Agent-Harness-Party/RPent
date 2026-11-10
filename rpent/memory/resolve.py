"""LLM-assisted resolution of conflicting memory notes.

A conflict appears when the same canonical note id is proposed with different
prose (common for ``task-family`` notes, whose id is per task, not per run).
This module asks an LLM to merge the two versions into one body while the
existing evidence fields are unioned by :func:`_merge_evidence`.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import httpx

from rpent.memory.manager import (
    MemoryManager,
    _merge_evidence,
    _render,
    _split_frontmatter,
)
from rpent.utils.logging import get_logger

logger = get_logger("memory")

SYSTEM = (
    "You merge two versions of the SAME memory note written by different robot "
    "exploration runs. Merge them into one concise technical note. Rules:\n"
    "1. Keep every distinct, actionable technique from both versions.\n"
    "2. If they contradict, prefer the version with stronger evidence "
    "(more cells, more solved seeds); mention the other only as a caveat.\n"
    "3. If one version is a strictly better/simpler solution, keep it and fold "
    "the useful details of the other into it.\n"
    "4. Never invent facts or numbers. Only use what is present.\n"
    "5. Output ONLY the Markdown body (no YAML frontmatter, no code fences).\n"
)


def _chat(base_url: str, api_key: str, model: str, prompt: str, timeout: float) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _find_canonical(root: Path, memory_id: str) -> Path | None:
    for scope in ("global", "task-family"):
        candidate = root / scope / f"{memory_id}.md"
        if candidate.is_file():
            return candidate
    return None


def _merge_prompt(meta_old: dict[str, Any], body_old: str,
                  meta_new: dict[str, Any], body_new: str) -> str:
    def ev(m: dict[str, Any]) -> str:
        e = m.get("evidence") or {}
        return json.dumps(
            {"cells": e.get("cells", []), "attempts": e.get("attempts"),
             "solved_seeds": e.get("solved_seeds", []),
             "failed_seeds": e.get("failed_seeds", [])},
            ensure_ascii=False,
        )

    return (
        f"# Version A (existing)\n"
        f"title: {meta_old.get('title', '')}\n"
        f"applies_when: {meta_old.get('applies_when', '')}\n"
        f"evidence: {ev(meta_old)}\n\n{body_old.strip()}\n\n"
        f"# Version B (new run)\n"
        f"title: {meta_new.get('title', '')}\n"
        f"applies_when: {meta_new.get('applies_when', '')}\n"
        f"evidence: {ev(meta_new)}\n\n{body_new.strip()}\n\n"
        "Return the merged Markdown body."
    )


def resolve_conflicts(
    root: str | Path,
    *,
    base_url: str,
    model: str,
    api_key: str,
    apply: bool = False,
    timeout: float = 120.0,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Merge every ``_internal/conflicts/*.md`` into its canonical note.

    Without ``apply`` this is a dry run: bodies are produced and returned but
    nothing on disk changes.
    """
    mgr = MemoryManager(root)
    root = mgr.root
    conflicts_dir = root / "_internal" / "conflicts"
    conflict_files = sorted(
        p for p in conflicts_dir.glob("*.md") if p.is_file()
    )
    if limit is not None:
        conflict_files = conflict_files[:limit]

    results: list[dict[str, Any]] = []
    for path in conflict_files:
        name = path.name
        memory_id = name.split("__from_", 1)[0]
        try:
            meta_new, body_new = _split_frontmatter(path)
        except ValueError as exc:
            results.append({"conflict": name, "status": "skipped", "reason": str(exc)})
            continue

        canonical = _find_canonical(root, memory_id)
        if canonical is None:
            scope = meta_new.get("scope", "global")
            target = root / scope / f"{memory_id}.md"
            if apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_render(meta_new, body_new))
                path.unlink()
            results.append({"conflict": name, "status": "adopted",
                            "target": str(target.relative_to(root))})
            continue

        try:
            meta_old, body_old = _split_frontmatter(canonical)
        except ValueError as exc:
            results.append({"conflict": name, "status": "skipped",
                            "reason": f"canonical unreadable: {exc}"})
            continue

        try:
            merged_body = _chat(
                base_url, api_key, model,
                _merge_prompt(meta_old, body_old, meta_new, body_new),
                timeout,
            )
        except Exception as exc:  # network / API errors must not destroy data
            results.append({"conflict": name, "status": "error", "reason": str(exc)})
            continue

        merged_meta = _merge_evidence(meta_old, meta_new)
        results.append({
            "conflict": name,
            "status": "merged",
            "applied": apply,
            "confidence": merged_meta.get("confidence"),
            "cells": (merged_meta.get("evidence") or {}).get("cells", []),
            "preview": merged_body[:200],
        })

        if apply:
            from rpent.memory.manager import _validate
            merged_meta["id"] = memory_id
            _validate(merged_meta)
            canonical.write_text(_render(merged_meta, merged_body + "\n"))
            resolved = conflicts_dir / "resolved"
            resolved.mkdir(exist_ok=True)
            shutil.move(str(path), str(resolved / name))

    if apply:
        mgr.rebuild_index()
    return results
