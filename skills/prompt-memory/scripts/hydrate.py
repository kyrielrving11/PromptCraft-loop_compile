"""Workspace-anchored prompt memory: hydrate context from .promptcraft/prompt_vault.json
and prompt .md files.

Dual storage:
  - .promptcraft/prompt_vault.json           ← lightweight metadata index
  - .promptcraft/prompts/<task_id>/<vN>.md   ← complete prompt (full text)

Usage:
  # Default: semantic search, returns summary (no raw prompt text)
  python hydrate.py --query "audit smart contract permissions" --top-k 3

  # Full mode: read complete prompt from linked .md files
  python hydrate.py --query "audit smart contract" --full

  # Auto-inject full prompt when score > threshold (default 0.75)
  python hydrate.py --query "audit smart contract" --auto-full-threshold 0.6

  # Filter by task_id or skill
  python hydrate.py --query "..." --task-id "smart-contract-audit" --skill "tree-of-thought"

  # Rollback: switch active version for a task
  python hydrate.py --rollback-to v1 --task-id "smart-contract-audit"

  # List version history for a task
  python hydrate.py --list-versions --task-id "smart-contract-audit"

  # Skip global vault (project-only search)
  python hydrate.py --query "..." --no-global

The global vault (~/.promptcraft/global_vault.json) is automatically merged
with the project vault during search. Use --no-global to disable this.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_VAULT = Path(".promptcraft/prompt_vault.json")
DEFAULT_PROMPTS_DIR = Path(".promptcraft/prompts")
GLOBAL_VAULT = Path.home() / ".promptcraft" / "global_vault.json"
GLOBAL_PROMPTS_DIR = Path.home() / ".promptcraft" / "prompts"

# Weight multipliers for scoring fields
_WEIGHTS = {
    "user_intent": 2.0,
    "tags": 2.0,
    "hard_constraints": 1.0,
    "key_decisions": 1.0,
    "execution_feedback": 0.5,
}

# Additional weights for summary sub-fields (nested under entry["summary"])
_SUMMARY_WEIGHTS = {
    "summary_text": 2.0,
    "goal": 1.5,
    "key_decisions": 1.0,
    "hard_constraints_added": 1.0,
    "what_was_done": 0.8,
    "important_outputs": 0.5,
    "open_questions": 0.5,
    "rejected_directions": 0.5,
}

# Default score threshold for auto-injecting full prompt text
DEFAULT_AUTO_FULL_THRESHOLD = 0.75
FRESHNESS_STALE_DAYS = 30       # Entries older than this get a freshness penalty
FRESHNESS_PENALTY_FACTOR = 0.7  # Multiply score by this factor for stale entries


def _read_vault(path: Path) -> dict:
    """Read the vault file with graceful error handling."""
    if not path.exists():
        return {"version": "1", "entries": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(json.dumps({"status": "warning", "message": f"Vault is corrupted ({exc}). Starting with empty vault."}))
        return {"version": "1", "entries": []}
    if not isinstance(data, dict):
        print(json.dumps({"status": "warning", "message": "Vault is not a JSON object. Starting with empty vault."}))
        return {"version": "1", "entries": []}
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    # Filter out malformed entries (non-dict)
    data["entries"] = [e for e in data["entries"] if isinstance(e, dict)]
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
    """Multi-script tokenizer — splits on word boundaries for common scripts."""
    tokens: set[str] = set()
    # CJK Unified Ideographs (common + Extension A): Chinese, Japanese kanji
    tokens.update(re.findall(r"[一-鿿㐀-䶿]", text))
    # Japanese Hiragana + Katakana
    tokens.update(re.findall(r"[぀-ゟ゠-ヿ]", text))
    # Korean Hangul syllables
    tokens.update(re.findall(r"[가-힯]", text))
    # Latin (with diacritics) + Cyrillic
    tokens.update(t.lower() for t in re.findall(r"[A-Za-zÀ-ɏЀ-ӿ]+", text))
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
    # Extract summary sub-fields for scoring
    summary = entry.get("summary")
    if isinstance(summary, dict):
        for field in _SUMMARY_WEIGHTS:
            value = summary.get(field, "")
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
    # Score summary sub-fields
    summary = entry.get("summary")
    if isinstance(summary, dict):
        for field, weight in _SUMMARY_WEIGHTS.items():
            value = summary.get(field, "")
            if isinstance(value, list):
                field_text = " ".join(str(v) for v in value)
            else:
                field_text = str(value) if value else ""
            if field_text:
                score += _jaccard(query_tokens, _tokenize(field_text)) * weight

    # ── Freshness penalty: stale entries (>30 days) get score reduced ──
    timestamp = entry.get("timestamp", "")
    if timestamp:
        try:
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_old = (now - ts).days
            if days_old > FRESHNESS_STALE_DAYS:
                score *= FRESHNESS_PENALTY_FACTOR
        except ValueError:
            pass  # Unparseable timestamp — no penalty (conservative)

    return round(score, 4)


def _freshness(timestamp: str) -> str:
    """Return human-readable age. 'today', 'yesterday', '47 days ago'."""
    if not timestamp:
        return "unknown"
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days = (now - ts).days
        if days == 0:
            return "today"
        if days == 1:
            return "yesterday"
        return f"{days} days ago"
    except ValueError:
        return "unknown"


def _freshness_warning(timestamp: str) -> str:
    """Freshness warning for entries > 1 day old (Claude Code pattern).

    Memories are point-in-time observations, not live state.
    Claims about code behavior or file locations may be outdated.
    """
    days_str = _freshness(timestamp)
    if days_str in ("today", "yesterday", "unknown"):
        return ""
    return (
        f"This vault entry is {days_str} old. "
        "Memories are point-in-time observations, not live state — "
        "claims about code behavior or file locations may be outdated. "
        "Verify against current code before asserting as fact."
    )


def _compact_entry(entry: dict, *, include_prompt: bool = False) -> dict:
    """Return a compact metadata view. When include_prompt=True, read full prompt from .md file.

    Default (include_prompt=False): returns summary if present; falls back to generated_prompt_preview
    for legacy entries without a summary. Raw prompt text is NOT exposed in default mode.
    """
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
    # Always include summary if present (privacy-safe: no raw prompt text)
    if entry.get("summary"):
        compact["summary"] = entry["summary"]
    elif not include_prompt:
        # Legacy fallback: old entries without summary → show preview for context
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
        # Include preview alongside full prompt for quick identification
        if entry.get("generated_prompt_preview"):
            compact["generated_prompt_preview"] = entry["generated_prompt_preview"]

    # Freshness: human-readable age + warning for entries > 1 day
    if entry.get("timestamp"):
        compact["freshness"] = _freshness(entry["timestamp"])
        warning = _freshness_warning(entry["timestamp"])
        if warning:
            compact["freshness_warning"] = warning

    return {k: v for k, v in compact.items() if v not in (None, "", [], 0) or k in ("score",)}


def _is_global(entry: dict) -> bool:
    """Check if an entry has importance: GLOBAL in its summary."""
    summary = entry.get("summary")
    return isinstance(summary, dict) and summary.get("importance") == "GLOBAL"


def _merge_global_entries(project_entries: list[dict], global_vault: dict | None) -> list[dict]:
    """Merge global vault entries into the working set.

    Rules:
    - Only active entries from the global vault are merged.
    - If a task_id already has an active entry in the project vault, the global
      entry is skipped (project overrides global).
    - Merged entries are tagged with source="global"; project entries get
      source="project".
    - The global entries are NOT written into the project vault dict — they only
      live in the returned list for this query.
    """
    merged = list(project_entries)
    if global_vault is None:
        for e in merged:
            e.setdefault("source", "project")
        return merged

    project_active_tids = {e.get("task_id") for e in project_entries if e.get("is_active")}

    for e in global_vault.get("entries", []):
        if not e.get("is_active", False):
            continue
        tid = e.get("task_id")
        if tid and tid in project_active_tids:
            continue  # project vault already has an active version for this task
        e_copy = dict(e)
        e_copy["source"] = "global"
        merged.append(e_copy)

    for e in merged:
        e.setdefault("source", "project")

    return merged


def cmd_query(args, vault: dict) -> None:
    query = str(args.query or "").strip()
    if not query:
        print(json.dumps({"status": "error", "message": "--query is required."}))
        sys.exit(1)

    query_tokens = _tokenize(query)

    # ── Merge global vault (unless --no-global) ──
    use_global = not getattr(args, "no_global", False)
    global_vault = None
    if use_global:
        global_vault = _read_vault(GLOBAL_VAULT) if GLOBAL_VAULT.exists() else None

    all_entries = _merge_global_entries(vault.get("entries", []), global_vault)
    entries = list(all_entries)

    # Filter if specified (filters apply to regular results, not GLOBAL)
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
    always_full = bool(getattr(args, "full", False))
    auto_threshold = float(getattr(args, "auto_full_threshold", DEFAULT_AUTO_FULL_THRESHOLD))

    # ── GLOBAL entries: always returned, regardless of query match ──
    # GLOBAL entries are drawn from ALL active entries (unfiltered), because
    # GLOBAL means "cross-task long-term constraints that every session must know."
    global_ids: set[str] = set()
    global_entries: list[dict] = []
    for e in all_entries:
        if e.get("is_active", False) and _is_global(e):
            eid = e.get("id", "")
            global_ids.add(eid)
            # Score GLOBAL entries too (for auto_full logic)
            if "score" not in e:
                e["score"] = _score(query_tokens, e)
            entry_score = e.get("score", 0)
            include_prompt = always_full or entry_score >= auto_threshold
            compact = _compact_entry(e, include_prompt=include_prompt)
            compact["auto_full"] = include_prompt and not always_full
            compact["global"] = True
            compact["source"] = e.get("source", "project")
            global_entries.append(compact)

    # ── Regular results: top-k scored entries, excluding those already in GLOBAL ──
    results: list[dict] = []
    for e in ranked[:top_k]:
        if e.get("id") in global_ids:
            continue  # already included in global_entries
        entry_score = e.get("score", 0)
        include_prompt = always_full or entry_score >= auto_threshold
        compact = _compact_entry(e, include_prompt=include_prompt)
        compact["auto_full"] = include_prompt and not always_full
        compact["global"] = False
        compact["source"] = e.get("source", "project")
        results.append(compact)

    output: dict[str, object] = {
        "status": "ok",
        "query": query,
        "auto_full_threshold": auto_threshold,
        "global_entries": global_entries,
        "results": results,
        "total_active_tasks": len(active_map),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


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


def cmd_aggregate(args, vault: dict) -> None:
    """Aggregate entries by a field, computing stats for Pattern Analysis.

    Three-tier gating hints are attached based on record counts:
      - >= 10: pattern_ready (internal observation)
      - >= 20 with >= 35% low-quality: evolution_ready (Skill change suggested)
      - >= 30: creation_ready (new Skill suggested)
    """
    group_by = str(getattr(args, "group_by", "task_type") or "task_type")
    min_records = int(getattr(args, "min_records", 10) or 10)
    task_type_filter = getattr(args, "task_type", None)

    entries = vault.get("entries", [])

    if task_type_filter:
        entries = [e for e in entries if e.get("task_type") == task_type_filter]

    # Group entries
    groups: dict[str, list[dict]] = {}
    for e in entries:
        key = str(e.get(group_by, "")).strip()
        if not key:
            continue
        groups.setdefault(key, []).append(e)

    # Compute stats per group
    results: list[dict] = []
    for key, group_entries in sorted(groups.items()):
        total = len(group_entries)
        if total < min_records:
            continue

        scores = [
            e["quality_score"]
            for e in group_entries
            if isinstance(e.get("quality_score"), (int, float)) and e["quality_score"] > 0
        ]
        avg_quality = round(sum(scores) / len(scores), 2) if scores else 0

        # High-frequency overlays (>= 50% of records in this group)
        overlay_counts: dict[str, int] = {}
        for e in group_entries:
            for ov in e.get("overlay_used", []):
                overlay_counts[ov] = overlay_counts.get(ov, 0) + 1
        high_freq = [
            {"overlay": ov, "count": c, "pct": round(c / total * 100)}
            for ov, c in overlay_counts.items()
            if c / total >= 0.5
        ]
        high_freq.sort(key=lambda x: x["count"], reverse=True)

        # Low-quality ratio (score < 3)
        low_count = sum(1 for s in scores if s < 3)
        low_ratio = round(low_count / len(scores), 2) if scores else 0

        # Latest entry timestamp
        timestamps = [
            e["timestamp"]
            for e in group_entries
            if e.get("timestamp")
        ]
        latest = max(timestamps) if timestamps else ""

        # Three-tier gating
        if total >= 30:
            gate = "creation_ready"
        elif total >= 20 and low_ratio >= 0.35:
            gate = "evolution_ready"
        elif total >= 10:
            gate = "pattern_ready"
        else:
            gate = "insufficient"

        results.append({
            "group_key": key,
            "total_records": total,
            "avg_quality": avg_quality,
            "high_freq_overlays": high_freq,
            "low_quality_ratio": low_ratio,
            "latest_timestamp": latest,
            "gate": gate,
        })

    results.sort(key=lambda r: r["total_records"], reverse=True)

    print(json.dumps({
        "status": "ok",
        "aggregate_by": group_by,
        "min_records": min_records,
        "groups": len(results),
        "results": results,
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hydrate prompt context from the vault.")
    parser.add_argument("--query", help="Query text for semantic search.")
    parser.add_argument("--top-k", type=int, default=3, help="Max results to return (default: 3).")
    parser.add_argument("--task-id", help="Filter results by task_id.")
    parser.add_argument("--skill", help="Filter results by skill_used.")
    parser.add_argument("--full", action="store_true", help="Always read complete prompt from linked .md files.")
    parser.add_argument("--auto-full-threshold", type=float, default=DEFAULT_AUTO_FULL_THRESHOLD,
                        help=f"Score threshold above which full prompt is auto-injected (default: {DEFAULT_AUTO_FULL_THRESHOLD}).")
    parser.add_argument("--no-global", action="store_true", help="Skip the global vault (~/.promptcraft/global_vault.json) — only search the project vault.")
    parser.add_argument("--aggregate", action="store_true", help="Aggregate query mode: group entries by a field and compute stats for Pattern Analysis.")
    parser.add_argument("--group-by", default="task_type", choices=["task_type", "skill_used", "technique"],
                        help="Field to group by in aggregate mode (default: task_type).")
    parser.add_argument("--min-records", type=int, default=10,
                        help="Only return groups with at least this many records (default: 10).")
    parser.add_argument("--task-type", help="Filter by task_type in aggregate mode.")
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
    elif args.aggregate:
        cmd_aggregate(args, vault)
    elif args.query:
        cmd_query(args, vault)
    else:
        print(json.dumps({"status": "error", "message": "Provide --query, --aggregate, --rollback-to, or --list-versions."}))
        sys.exit(1)


if __name__ == "__main__":
    main()
