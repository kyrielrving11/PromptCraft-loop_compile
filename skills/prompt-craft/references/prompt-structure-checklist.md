# Prompt Structure Checklist

After building an enhanced prompt, verify it covers these elements before presenting
to the user. Not all elements are required for every task — skip those that don't apply.

## Core Elements

- [ ] **Role**: Is a clear role assigned? (e.g. "You are a senior Solidity auditor")
- [ ] **Task**: Is the specific task stated unambiguously?
- [ ] **Input**: Is the target input (code, data, file) specified?
- [ ] **Output Format**: Is the desired output structure defined (JSON, Markdown, code block, plain text)?

## Quality Elements

- [ ] **Hard Constraints**: Are non-negotiable rules explicitly stated (e.g. "do NOT introduce tokenomics analysis")?
- [ ] **Negative Constraints**: Are forbidden approaches or outputs listed?
- [ ] **Examples**: If few-shot, are 1-5 input→output (or input→reasoning→output) examples included?
- [ ] **Reasoning Structure**: If CoT/ToT, is the reasoning→answer format clearly separated?
- [ ] **Pruning Criteria**: If ToT, are branch evaluation and pruning criteria explicit?

## Context Elements

- [ ] **Irrelevant History Excluded**: For independent high-load tasks, is prior conversation noise omitted?
- [ ] **Vault Constraints Injected**: Are relevant hard_constraints from the vault included?
- [ ] **Technique Alignment**: Does the prompt's structure match the selected technique's method_steps?

## Technique Alignment

After construction, verify the prompt matches the output template of the selected technique. Each reference file now includes a **Prompt Output Template** section that defines the exact output skeleton.

- [ ] **Zero-Shot**: Prompt is light (≤100 lines), no examples or reasoning frames. Only 7 sections (section 5 omitted).
- [ ] **Few-Shot**: Section 5 is "格式参考示例（Few-Shot）" with 2-3 input→output pairs + mapping rule summary box. Examples are task-domain real data, NOT meta-examples of prompt design.
- [ ] **Zero-Shot-CoT**: Section 5 is a reasoning skeleton (format hint only, no concrete reasoning content). "先推理 → 再答案" structure.
- [ ] **Few-Shot-CoT**: Section 5 is "推理模式参考" with 2 input→reasoning→output triples + reasoning pattern migration box.
- [ ] **Step-Back**: Section 5 contains 2-3 abstraction framework ASCII boxes. Section 6 starts with transition sentence "基于上述抽象框架，实现以下所有功能".
- [ ] **Least-to-Most**: Section 5 contains 4-6 ordered subproblems (目标→要求→示例). Last subproblem is "综合实现完整模块". Section 6 expands by output format items, not by subproblems.
- [ ] **Tree-of-Thought**: Section 5 includes search strategy declaration, evaluation criteria table, and thought-tree state table format. Branch count ≤4, depth ≤3.

## Anti-Patterns to Avoid

- [ ] No generic advice without concrete application ("be careful" without saying HOW).
- [ ] No hidden reasoning chains that the end-user model can't see.
- [ ] No mixed unrelated tasks in a single prompt.
- [ ] No implicit assumptions about the user's environment or stack.
