# Prompt Review Checklist

Audit categories for reviewing an enhanced prompt.

## 1. Completeness

- [ ] **Role**: Is a specific, actionable role assigned? (Not "you are helpful")
- [ ] **Task**: Is the task stated unambiguously with concrete scope?
- [ ] **Input**: Is the target input (code, file, data) clearly identified?
- [ ] **Output Format**: Is the output structure explicitly specified (JSON schema, Markdown, code block)?

## 2. Constraints

- [ ] **Hard Constraints**: Are non-negotiable rules explicitly stated with "do NOT" or "must" language?
- [ ] **Negative Constraints**: Are forbidden approaches or outputs explicitly listed?
- [ ] **Scope Boundaries**: Is what the model should NOT do clearly defined?

## 3. Technique Fit

- [ ] **Method Steps Match**: Does the prompt follow the selected technique's method_steps?
- [ ] **Examples**: If few-shot/few-shot-cot, are examples formatted as proper input→output or input→reasoning→output triples?
- [ ] **Reasoning Structure**: If CoT/ToT, is reasoning clearly separated from the final answer?
- [ ] **Pruning**: If ToT, are branch evaluation and pruning criteria explicit?

## 4. Context Quality

- [ ] **No Irrelevant History**: For independent high-load tasks, is prior conversation noise excluded?
- [ ] **No Hidden Assumptions**: Is the prompt self-contained — can a fresh model understand it without session context?
- [ ] **Vault Constraints**: Are relevant hard_constraints from the vault included?

## 5. Anti-Patterns

- [ ] **No Generic Advice**: Every instruction has a concrete "how" — not just "be careful"
- [ ] **No Mixed Tasks**: A single prompt handles exactly one task
- [ ] **No Implicit Environment**: Any environment assumptions are stated
- [ ] **No Buried Answers**: For multi-step reasoning, the final answer is clearly marked and extractable

## 6. Edge Cases & Safety

- [ ] **Security**: If involving auth, crypto, or permissions — are security boundaries explicit?
- [ ] **Error Handling**: Does the prompt specify what to do on invalid input?
- [ ] **Ambiguity**: Are vague terms ("good", "proper", "nice") replaced with concrete criteria?

## Severity Tags

Use these tags when reporting findings:
- **[BLOCKER]**: Missing core element (no role, no task, no input)
- **[MAJOR]**: Technique mismatch, critical constraint missing
- **[MINOR]**: Style improvement, optional enhancement
