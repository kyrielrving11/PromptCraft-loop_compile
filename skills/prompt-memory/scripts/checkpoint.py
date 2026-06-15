"""Workpace-anchored prompt memory: save prompt contexts.

Dual storage:
  - .promptcraft/prompts/<task_id>/<version_tag>.md  ← complete prompt (Markdown, human-readable)
  - .promptcraft/prompt_vault.json                    ← lightweight index (metadata + md_path + preview only)

Usage:
  # First save for a new task
  echo '{"task_id":"my-task","skill_used":"zero-shot","user_intent":"..."}' | python checkpoint.py

  # Create a new version for the same task (auto-increments version_tag, updates is_active)
  echo '{"task_id":"my-task","skill_used":"tree-of-thought","user_intent":"..."}' | python checkpoint.py --version-of my-task

  # From file
  python checkpoint.py --input entry.json
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REQUIRED_KEYS = {"task_id", "user_intent"}
_OPTIONAL_KEYS = {
    "skill_used", "stage", "hard_constraints", "key_decisions",
    "generated_prompt", "execution_feedback", "tags",
}

DEFAULT_VAULT = Path(".promptcraft/prompt_vault.json")
DEFAULT_PROMPTS_DIR = Path(".promptcraft/prompts")
MAX_PREVIEW_CHARS = 200


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _truncate(text: str, max_chars: int = MAX_PREVIEW_CHARS) -> str:
    text = str(text or "").strip()
    return text if len(text) <= max_chars else text[:max_chars] + "..."


def _read_vault(path: Path) -> dict:
    if not path.exists():
        return {"version": "1", "entries": []}
    with path.open("r", encoding="utf-8") as f:
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


def _find_active(vault: dict, task_id: str) -> dict | None:
    for entry in vault["entries"]:
        if entry.get("task_id") == task_id and entry.get("is_active"):
            return entry
    return None


def _count_versions(vault: dict, task_id: str) -> int:
    return sum(1 for e in vault["entries"] if e.get("task_id") == task_id)


def _validate_entry(entry: dict) -> None:
    for key in _REQUIRED_KEYS:
        if not str(entry.get(key, "")).strip():
            raise ValueError(f"Missing required field: {key}")


def _write_prompt_md(prompts_dir: Path, task_id: str, version_tag: str, content: str) -> str:
    """Write full prompt to a .md file. Returns the relative path."""
    md_dir = prompts_dir / task_id
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / f"{version_tag}.md"
    with md_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(content.strip())
        f.write("\n")
    return str(md_path.as_posix())


def _build_entry(payload: dict, vault: dict, prompts_dir: Path, version_of: str | None) -> dict:
    _validate_entry(payload)

    full_prompt = str(payload.get("generated_prompt", "")).strip()

    entry = {
        "id": str(uuid.uuid4()),
        "task_id": str(payload["task_id"]).strip(),
        "version_tag": "v1",
        "is_active": True,
        "parent_version": None,
        "timestamp": _utc_now(),
        "skill_used": str(payload.get("skill_used", "")).strip(),
        "user_intent": str(payload.get("user_intent", "")).strip(),
        "hard_constraints": _list_field(payload, "hard_constraints"),
        "key_decisions": _list_field(payload, "key_decisions"),
        "generated_prompt_preview": _truncate(full_prompt) if full_prompt else "",
        "execution_feedback": str(payload.get("execution_feedback", "")).strip(),
        "tags": _list_field(payload, "tags"),
    }

    # Write full prompt to .md file, store only the path reference
    if full_prompt:
        entry["md_path"] = _write_prompt_md(prompts_dir, entry["task_id"], entry["version_tag"], full_prompt)

    task_id = entry["task_id"]
    if version_of and version_of == task_id:
        active = _find_active(vault, task_id)
        count = _count_versions(vault, task_id)
        entry["version_tag"] = f"v{count + 1}"
        entry["parent_version"] = active["version_tag"] if active else None
        # Re-write md_path with the correct version_tag
        if full_prompt:
            entry["md_path"] = _write_prompt_md(prompts_dir, entry["task_id"], entry["version_tag"], full_prompt)
        for e in vault["entries"]:
            if e.get("task_id") == task_id:
                e["is_active"] = False
        vault["entries"].append(entry)
    else:
        count = _count_versions(vault, task_id)
        entry["version_tag"] = f"v{count + 1}" if count > 0 else "v1"
        # Re-write md_path with correct version_tag (in case count > 0)
        if full_prompt:
            entry["md_path"] = _write_prompt_md(prompts_dir, entry["task_id"], entry["version_tag"], full_prompt)
        vault["entries"].append(entry)

    return entry


def _list_field(payload: dict, key: str) -> list[str]:
    value = payload.get(key, [])
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Save a prompt checkpoint to the vault.")
    parser.add_argument("--input", type=Path, help="JSON file with entry payload.")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT, help="Path to prompt_vault.json.")
    parser.add_argument("--prompts-dir", type=Path, default=DEFAULT_PROMPTS_DIR, help="Directory for .md prompt files.")
    parser.add_argument("--version-of", help="task_id to create a new version for.")
    args = parser.parse_args()

    if args.input:
        with args.input.open("r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    else:
        raw = sys.stdin.buffer.read().decode("utf-8-sig").strip()
        if not raw:
            print(json.dumps({"status": "error", "message": "No input provided."}))
            sys.exit(1)
        payload = json.loads(raw)

    if not isinstance(payload, dict):
        print(json.dumps({"status": "error", "message": "Input must be a JSON object."}))
        sys.exit(1)

    vault = _read_vault(args.vault)
    try:
        entry = _build_entry(payload, vault, args.prompts_dir, args.version_of)
    except ValueError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    _write_vault(args.vault, vault)
    result = {
        "status": "saved",
        "id": entry["id"],
        "version_tag": entry["version_tag"],
        "is_active": entry["is_active"],
        "entries_count": len(vault["entries"]),
    }
    if entry.get("md_path"):
        result["md_path"] = entry["md_path"]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
