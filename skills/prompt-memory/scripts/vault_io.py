"""Shared vault I/O helpers — used by both checkpoint.py and hydrate.py.

Extracted to a single source of truth so _read_vault and _write_vault are
defined once, not copy-pasted across two scripts.
"""

import json
from pathlib import Path


def read_vault(path: Path) -> dict:
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


def write_vault(path: Path, data: dict) -> None:
    """Write the vault file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
