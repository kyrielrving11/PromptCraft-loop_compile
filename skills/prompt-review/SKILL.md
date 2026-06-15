---
name: prompt-review
description: >
  Prompt quality review and improvement. This skill should be used when the
  user wants to check an existing prompt for completeness, identify missing
  constraints or anti-patterns, and produce an improved version. The improved
  version is saved as a new version to the vault — never overwriting the
  original.
---

# Prompt Review & Improvement

This skill audits an existing enhanced prompt for completeness,
anti-patterns, and missing elements. Improved versions are appended
as new entries to the vault via checkpoint.py — the original is preserved.

## Workflow

### Step 1: Load the Prompt

Identify the prompt to review. This could be:
- A prompt just produced by `prompt-craft` in the current session.
- A prompt loaded from the vault via `hydrate.py`.
- A prompt the user pastes directly.

### Step 2: Audit Against Checklist

Read `references/review-checklist.md`. Audit the prompt against each category:

- **Completeness**: Role, task, input, output format present?
- **Constraints**: Hard constraints explicit? Negative constraints listed?
- **Anti-Patterns**: Generic advice without concrete application? Hidden reasoning?
- **Technique Fit**: Does the prompt's structure match the technique's method_steps?

### Step 3: Report Findings

Present findings as a structured report:
1. **Passing**: Elements the prompt handles well.
2. **Missing**: Elements that should be added.
3. **Risky**: Anti-patterns or potential issues.
4. **Suggestions**: Concrete rewrites or additions.

### Step 4: Offer Improvement

After the report, ask the user: "Apply improvements and save as a new version?"

If yes:
1. Produce the improved prompt.
2. Run checkpoint.py with `--version-of <task_id>` to save it as a new version.
3. Report the new version_tag.

```bash
echo '{"task_id":"...","skill_used":"...","user_intent":"...",...}' | \
  python .codebuddy/skills/prompt-memory/scripts/checkpoint.py --version-of <task_id>
```

## Notes

- The ORIGINAL version is NEVER overwritten. checkpoint.py always appends.
- If the prompt fails review, the report itself is valuable — save it as
  `execution_feedback` in the new version's checkpoint.
- This skill can be loaded independently (without prompt-craft) when reviewing
  an existing prompt from the vault or from the user.
