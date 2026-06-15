"""Workpace-anchored prompt memory: hydrate context from .promptcraft/prompt_vault.json
and prompt .md files.

Dual storage:
  - .promptcraft/prompt_vault.json           ← lightweight metadata index
  - .promptcraft/prompts/<task_id>/<vN>.md   ← complete prompt (full text)

Usage:
  # Default: semantic search, metadata only (compact — no prompt text)
  python hydrate.py --query "audit smart contract permissions" --top-k 3

  # Full mode: read complete prompt from linked .md files
  python hydrate.py --query "audit smart contract" --full

  # Filter by task_id or skill
  python hydrate.py --query "..." --task-id "smart-contract-audit" --skill "tree-of-thought"

  # Rollback: switch active version for a task
  python hydrate.py --rollback-to v1 --task-id "smart-contract-audit"

  # List version history for a task
  python hydrate.py --list-versions --task-id "smart-contract-audit"
"""

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_VAULT = Path(".promptcraft/prompt_vault.json")
DEFAULT_PROMPTS_DIR = Path(".promptcraft/prompts")

# Weight multipliers for scoring fields
_WEIGHTS = {
    "user_intent": 2.0,
    "tags": 2.0,
    "hard_constraints": 1.0,
    "key_decisions": 1.0,
    "execution_feedback": 0.5,
}


def _read_vault(path: Path) -> dict:
    if not path.exists():
        return {"version": "1", "entries": []}
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    data.setdefault("version", "1")
    return data


def _write_vault(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _read_prompt_md(md_path: str) -> str:
    """Read full prompt from the linked .md file."""
    md_file = Path(md_path)
    if not md_file.exists():
        return ""
    with md_file.open("r", encoding="utf-8") as f:
        return f.read().strip()


def _tokenize(text: str) -> set[str]:
    """Simple Chinese/English tokenizer — splits on word boundaries."""
    tokens: set[str] = set()
    tokens.update(re.findall(r"[\u4e00-\u9fff]", text))
    tokens.update(t.lower() for t in re.findall(r"[a-zA-Z]+", text))
    return tokens


def _jaccard(set_a: set[str], set_b: set[str]) -> float:
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _entry_text(entry: dict) -> str:
    parts: list[str] = []
    for field in _WEIGHTS:
        value = entry.get(field, "")
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts)


def _score(query_tokens: set[str], entry: dict) -> float:
    score = 0.0
    for field, weight in _WEIGHTS.items():
        value = entry.get(field, "")
        if isinstance(value, list):
            field_text = " ".join(str(v) for v in value)
        else:
            field_text = str(value) if value else ""
        if field_text:
            score += _jaccard(query_tokens, _tokenize(field_text)) * weight
    return round(score, 4)


def _compact_entry(entry: dict, *, include_prompt: bool = False) -> dict:
    """Return a compact metadata view. When include_prompt=True, read full prompt from .md file."""
    compact = {
        "id": entry["id"],
        "task_id": entry["task_id"],
        "version_tag": entry["version_tag"],
        "is_active": entry["is_active"],
        "parent_version": entry.get("parent_version"),
        "timestamp": entry["timestamp"],
        "skill_used": entry.get("skill_used", ""),
        "user_intent": entry.get("user_intent", ""),
        "hard_constraints": entry.get("hard_constraints", []),
        "key_decisions": entry.get("key_decisions", []),
        "execution_feedback": entry.get("execution_feedback", ""),
        "tags": entry.get("tags", []),
        "score": entry.get("score", 0),
    }
    # Always include preview and md_path for reference
    if entry.get("generated_prompt_preview"):
        compact["generated_prompt_preview"] = entry["generated_prompt_preview"]
    if entry.get("md_path"):
        compact["md_path"] = entry["md_path"]

    if include_prompt:
        # Read full prompt from .md file
        md_path = entry.get("md_path", "")
        if md_path:
            compact["generated_prompt"] = _read_prompt_md(md_path)
        else:
            # Fallback: legacy entries may still have prompt in JSON
            compact["generated_prompt"] = entry.get("generated_prompt", "")

    return {k: v for k, v in compact.items() if v not in (None, "", [], 0) or k in ("score", "generated_prompt_preview")}


def cmd_query(args, vault: dict) -> None:
    query = str(args.query or "").strip()
    if not query:
        print(json.dumps({"status": "error", "message": "--query is required."}))
        sys.exit(1)

    query_tokens = _tokenize(query)
    entries = vault.get("entries", [])

    # Filter if specified
    if args.task_id:
        entries = [e for e in entries if e.get("task_id") == args.task_id]
    if args.skill:
        entries = [e for e in entries if e.get("skill_used") == args.skill]

    # Score each entry
    for entry in entries:
        entry["score"] = _score(query_tokens, entry)

    # Only keep is_active version per task_id (highest score wins for tiebreaker)
    active_map: dict[str, dict] = {}
    for entry in entries:
        if entry.get("is_active", False):
            tid = entry["task_id"]
            if tid not in active_map or entry["score"] > active_map[tid]["score"]:
                active_map[tid] = entry

    # Sort by score desc, take top-k
    ranked = sorted(active_map.values(), key=lambda e: e.get("score", 0), reverse=True)
    top_k = min(int(args.top_k or 3), len(ranked))
    include_prompt = bool(getattr(args, "full", False))
    results = [_compact_entry(e, include_prompt=include_prompt) for e in ranked[:top_k]]

    print(json.dumps({
        "status": "ok",
        "query": query,
        "results": results,
        "total_active_tasks": len(active_map),
    }, ensure_ascii=False, indent=2))


def cmd_rollback(args, vault: dict) -> None:
    task_id = str(args.task_id or "").strip()
    version_tag = str(args.rollback_to or "").strip()
    if not task_id or not version_tag:
        print(json.dumps({"status": "error", "message": "--task-id and --rollback-to are required."}))
        sys.exit(1)

    found = False
    for entry in vault["entries"]:
        if entry.get("task_id") == task_id:
            if entry.get("version_tag") == version_tag:
                entry["is_active"] = True
                found = True
            else:
                entry["is_active"] = False

    if not found:
        print(json.dumps({"status": "error", "message": f"Version {version_tag} not found for task {task_id}."}))
        sys.exit(1)

    _write_vault(args.vault, vault)
    print(json.dumps({
        "status": "ok",
        "action": "rollback",
        "task_id": task_id,
        "active_version": version_tag,
    }))


def cmd_list_versions(args, vault: dict) -> None:
    task_id = str(args.task_id or "").strip()
    if not task_id:
        print(json.dumps({"status": "error", "message": "--task-id is required."}))
        sys.exit(1)

    versions = [
        {
            "version_tag": e["version_tag"],
            "is_active": e.get("is_active", False),
            "parent_version": e.get("parent_version"),
            "timestamp": e["timestamp"],
            "user_intent": e.get("user_intent", ""),
            "execution_feedback": e.get("execution_feedback", ""),
            "md_path": e.get("md_path", ""),
        }
        for e in vault["entries"]
        if e.get("task_id") == task_id
    ]
    versions.sort(key=lambda v: v["version_tag"])

    print(json.dumps({
        "status": "ok",
        "task_id": task_id,
        "versions": versions,
        "total_versions": len(versions),
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hydrate prompt context from the vault.")
    parser.add_argument("--query", help="Query text for semantic search.")
    parser.add_argument("--top-k", type=int, default=3, help="Max results to return (default: 3).")
    parser.add_argument("--task-id", help="Filter results by task_id.")
    parser.add_argument("--skill", help="Filter results by skill_used.")
    parser.add_argument("--full", action="store_true", help="Read complete prompt from linked .md files.")
    parser.add_argument("--rollback-to", help="Version tag to rollback to (requires --task-id).")
    parser.add_argument("--list-versions", action="store_true", help="List all versions for a task.")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT, help="Path to prompt_vault.json.")
    parser.add_argument("--prompts-dir", type=Path, default=DEFAULT_PROMPTS_DIR, help="Directory for .md prompt files.")
    args = parser.parse_args()

    vault = _read_vault(args.vault)

    if args.list_versions:
        cmd_list_versions(args, vault)
    elif args.rollback_to:
        cmd_rollback(args, vault)
    elif args.query:
        cmd_query(args, vault)
    else:
        print(json.dumps({"status": "error", "message": "Provide --query, --rollback-to, or --list-versions."}))
        sys.exit(1)


if __name__ == "__main__":
    main()
