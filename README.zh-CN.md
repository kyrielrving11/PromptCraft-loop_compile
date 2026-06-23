# PromptCraft-loop_compile

[English README](README.md)

PromptCraft-loop_compile 是面向 AI 编程助手的 **循环时智能编译层**
（Loop-Time Intelligence Layer，适用于 Claude Code / Codex / CodeBuddy）。
它的核心职责：为长程 Agent Loop 编译每轮迭代的提示词 —— 具备结构化记忆、
约束继承、漂移纠正和增量重编译（L0/L1/L2）能力。

> **v3.5.1** — 3 种对外模式。8 大核心机制。Vault 跨轮记忆。
> 约束退役、滚动摘要、自适应技术路由。
> 186 测试。Python 标准库，零外部依赖。

---

## 核心概念

```
Loop Runtime（Claude Code /loop、cron 等）
  │
  ├─ 第N轮：  调 PromptCraft loop_compile → 获取编译后的提示词 → 执行 → 反馈
  ├─ 第N+1轮：调 PromptCraft loop_compile（读 vault，打补丁或重建） → 执行
  └─ ...

PromptCraft 不是 Loop Runtime。它是 Runtime 在需要"知道上一轮发生了什么"
的提示词时，调用的智能层。
```

## 3 种模式

| 模式 | 触发条件 | 返回内容 |
|------|---------|---------|
| **loop_compile** | 每轮 agent loop 迭代 | 编译后的提示词 + 重编译级别(L0/L1/L2) + loop_objective + loop_health + task_alignment |
| **feedback** | 执行完成后 | 质量评分 → vault 持久化 |
| **review** | 审计提示词质量 | 结构检查 + 约束合规报告 |

`build` 为内部路径（loop_compile L2 委托 `builder.py` 做技术路由），不对外暴露。

## 重编译级别

| 级别 | 触发条件 | 行为 |
|------|---------|------|
| **L0 快速路径** | goal_id 不变，无新失败/约束 | 复用缓存提示词，轮次+1 |
| **L1 补丁** | 新约束、新失败、修复信号 | 增量补丁上一轮提示词；自动退役静默约束 |
| **L2 完整重编译** | 首轮、goal_id 变更、plan_source、策略崩溃 | 完整 hydrate + 自适应路由 + 滚动摘要 + build |

**硬门控**（可改变编译级别）：force_level 覆盖、首轮/plan_source、goal_id 变更、显式失败/约束。

**软建议**（仅告警，不阻断）：任务对齐度 vs Loop Objective、循环健康（漂移、约束完整性、策略稳定性）、修复信号检测、vault 前瞻提示。

**v3.5 新增**：L1 自动退役连续 3 轮无活跃信号的约束；L1/L2 注入滚动摘要（近 5 轮质量轨迹、有效做法、重复问题、关键教训）；L2 使用自适应技术路由（质量驱动的 fallback）。

## 快速开始

```bash
# 1. 复制核心目录到你的项目
cp -r loop-compiler/ skills/ .claude/ <你的项目>/

# 2. 初始化 vault
cd <你的项目>
echo '{"task_id":"init","user_intent":"promptcraft 已初始化"}' \
  | python skills/prompt-memory/scripts/checkpoint.py

# 3. 主模式：loop_compile
echo '{"mode":"loop_compile","loop_id":"test","round":1,"goal_id":"audit-erc20","task":"审计 ERC20 代币安全漏洞"}' \
  | python loop-compiler/subagent_adapter.py

# 4. 反馈模式
echo '{"task":"审计合约","mode":"feedback","feedback":{"output":"完成","success":true}}' \
  | python loop-compiler/subagent_adapter.py
```

## 架构

```
主 Agent (Claude Code / Codex)
  │
  └─ PromptCraft 子 Agent
        │
        └─ Python 层 (纯函数 + 生命周期)
            ├─ loop_compiler.py    ← decide_level + 软建议 + L0/L1/L2
            ├─ builder.py          ← 技术路由器 (关键词 + 自适应) + 质量评分
            ├─ engine.py           ← 生命周期 + vault I/O + 熔断器 + 反馈回写
            ├─ protocol.py         ← I/O schema (19 种类型)
            └─ subagent_adapter.py ← 统一入口，3 模式路由
```

## 核心特性（v3.5）

- **loop_compile**：每轮迭代提示词编译器 — L0/L1/L2 增量重编译 + 4 硬门控
- **Loop Objective 锚定**：首轮自动生成稳定目标锚点（3 种来源） — 防止跨轮目标漂移
- **约束退役（v3.5）**：连续 3 轮无活跃信号的约束自动退役到 `constraints_retired` — 防止长循环中提示词无限膨胀
- **滚动摘要（v3.5）**：确定性跨轮知识蒸馏 — 质量轨迹、有效做法、重复问题、关键教训 — 注入 L1/L2 提示词
- **自适应技术路由（v3.5）**：质量驱动的关键词路由 fallback — 同技术连续 2+ 轮低分则按 fallback 链旋转（如 zero-shot → few-shot）
- **反馈质量回写（v3.5.1）**：Feedback 质量分以 loop 感知的 task_id 写入 vault，hydrate 时回并到 lineage — 实现端到端自适应路由
- **跨轮 Vault 记忆**：每轮 lineage 持久化 → vault → 下轮 hydrate → get_previous_round()
- **双键目标身份**：goal_id（稳定语义主键）+ goal_text_hash（漂移检测，仅告警）
- **Task Alignment**：校验 Agent 提议的下一轮任务 vs Loop Objective — 区分合理演化与目标漂移
- **双向 Lineage 存储**：Vault JSON（主存储，可搜索）+ Markdown frontmatter with YAML（人类可读，git 友好，回退读取路径）。L0 缓存从 Markdown 文件复用真实缓存提示词
- **4 软建议**：任务对齐、循环健康、修复信号、前瞻提示 — 全部为告警，绝不硬阻断
- **熔断器**：纯函数趋势检测 — 连续 3 轮无改善 → STALLED。计数器仅在 feedback 事件时更新
- **多项目联邦**：双层 vault — 全局(`~/.promptcraft/`) + 项目(`./.promptcraft/`)
- **追加式 Vault**：完整版本历史，支持回滚，双向存储（JSON + Markdown frontmatter）
- **共享 vault I/O**：`vault_io.py` — `read_vault` / `write_vault` 的单一数据源，checkpoint 和 hydrate 共用
- **186 测试**，Python 标准库，零外部依赖

## 项目结构

```
PromptCraft-loop_compile/
├── loop-compiler/
│   ├── subagent_adapter.py    # 统一入口，3 模式路由
│   ├── engine.py              # 生命周期 + vault I/O + 熔断器
│   ├── loop_compiler.py       # 纯函数编译器：门控 + 建议 + L0/L1/L2
│   ├── builder.py             # 技术路由器 (关键词 + 自适应) + 质量评分
│   └── protocol.py            # I/O schema，19 种类型
├── skills/
│   ├── prompt-memory/         # 双存储 vault I/O + 联邦
│   │   └── scripts/           # checkpoint.py, hydrate.py, vault_io.py (共享 I/O)
│   └── prompt-techniques/     # 7 种技巧参考目录
├── tests/
│   ├── test_loop_compiler.py  # 94 测试：门控、建议、L0/L1/L2、约束退役、滚动摘要、自适应路由
│   ├── test_scripts.py        # 49 测试：checkpoint、hydrate、federation
│   ├── test_engine_modes.py   # 22 测试：invoke_*、YAML frontmatter、lineage md
│   ├── test_subagent_adapter.py # 7 测试：路由、解析、格式化
│   └── test_integration.py    # 9 测试：完整闭环工作流、熔断器、反馈回写
├── CLAUDE.md                  # 项目约定
└── README.md / README.zh-CN.md
```

## 设计原则

- **Python 分类，LLM 生成** — 技术选择用关键词启发式（快、零成本）；提示词写作由 LLM 驱动
- **Loop Objective 是锚点，不是规划器** — 冻结 what+why，不做工作分解
- **软建议绝不阻断** — task_alignment、loop_health、repair cues 仅为告警，非硬门控
- **增强而非替代** — Skill 拥有工作流，PromptCraft 提供叠加
- **Fail-closed** — 守卫不确定就拒绝
- **绝不自动修改 Skill** — 仅建议，执行由主 Agent 负责
- **追加不覆盖** — 完整版本历史保留
- **零外部依赖** — 纯文件系统，人类可读 JSON/Markdown

## 许可证

MIT License。详见 [LICENSE](LICENSE)。
