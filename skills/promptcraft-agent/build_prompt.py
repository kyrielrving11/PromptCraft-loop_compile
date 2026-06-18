"""Build the final Agent system prompt from the 7-layer template.

Reads SYSTEM_PROMPT.md from the same directory and replaces <placeholder>
variables with runtime values. Outputs the complete system prompt to stdout.

Usage:
  python build_prompt.py --skills-dir .codebuddy/skills
  python build_prompt.py --skills-dir .codebuddy/skills --platform linux
  python build_prompt.py --skills-dir skills --output agent_prompt.txt
"""

import argparse
import platform as _platform
from datetime import date
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent / "SYSTEM_PROMPT.md"


def build(skills_dir: str, platform_str: str | None = None) -> str:
    """Read the template and inject runtime variables."""
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Template not found: {TEMPLATE_PATH}")

    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    pf = platform_str or f"{_platform.system()} {_platform.machine()}"

    return (
        template
        .replace("<platform>", pf)
        .replace("<skills_dir>", skills_dir)
        .replace("<date>", date.today().isoformat())
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the PromptCraft Agent system prompt from the 7-layer template."
    )
    parser.add_argument(
        "--skills-dir",
        required=True,
        help="Path to the skills directory (e.g. .codebuddy/skills or skills).",
    )
    parser.add_argument(
        "--platform",
        default=None,
        help="Override platform string. Default: auto-detect.",
    )
    parser.add_argument(
        "--output",
        default=None,
        type=Path,
        help="Write to file instead of stdout.",
    )
    args = parser.parse_args()

    prompt = build(args.skills_dir, args.platform)

    if args.output:
        args.output.write_text(prompt, encoding="utf-8")
        print(f"System prompt written to {args.output}")
    else:
        # Force UTF-8 to avoid Windows GBK encoding errors
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
        print(prompt)


if __name__ == "__main__":
    main()
