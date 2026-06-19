# PromptCraft

[English README](README.md)

PromptCraft 是一个面向 AI 编程助手的**提示工程子Agent**
（Claude Code / Codex / CodeBuddy）。它管理提示词和技能的全生命周期：
生成、个性化、执行反馈、模式分析和进化建议——由持久化 vault 支撑，
跨会话、跨项目持续改进。

> **v2.6** — 子Agent架构：6种模式、5层执行边界、双存储vault、批处理、
> 主动信号、vault-hydrate预检门控、查询扩展、vault清理、引擎指标、182个测试。Python 标准库，零外部依赖。

---

## 架构

```
主Agent (Claude Code / Codex)
  │
  ├─ promptcraft-bridge (触发器Skill)  ← when_to_use + vault hydrate 预检
  │     └─ 按需委托给 PromptCraft 子Agent
  │
  └─ PromptCraft 子Agent (隔离上下文)
        │
        ├─ subagent_adapter.py   ← 统一入口，6模式路由
        ├─ engine.py             ← 生命周期管理 + 熔断器、批处理
        ├─ boundary.py           ← 5层纵深防御
        ├─ circuit_breaker.py    ← 拒绝追踪，3态状态机
        └─ tools/                ← 5个专用引擎
              personalization / prompt_build / feedback_collect
              / pattern_analysis / skill_advisor
```

## 六种模式

| 模式 | 触发条件 | 返回内容 |
|------|---------|---------|
| **overlay** | 匹配Skill + vault有相关历史 | 领域过滤后的约束叠加 |
| **build** | 无Skill + 高风险任务，或需建立vault基线 | 完整8节结构化提示词 |
| **feedback** | 执行完成后 | 质量评分 + 改进建议 |
| **analyze** | Health Report 信号 `->analyze` | 累积数据的模式报告 |
| **advise** | Health Report 信号 `->advise` | Skill进化/创建建议 |
| **batch** | 批量任务 | BatchSummary + 逐项结果 |

**触发模型**：`when_to_use`（LLM语义判断）→ 低成本 vault hydrate
(`hydrate.py --query <task> --top 3`) → 有相关历史或高风险关键词
→ 调用 overlay/build。否则跳过 PromptCraft。无 assess 子Agent往返。

每次响应附紧凑 **Health Report**：`[PC: 15 records, normal]`
以及 `proactive_signals`——vault 上下文提示（类似任务、常见陷阱）。

## 快速开始

将 PromptCraft 部署为 Claude Code 子Agent：

```bash
# 1. 复制 3 个核心目录到你的项目
cp -r promptcraft-agent/ skills/ .claude/ <你的项目>/

# 2. 初始化 vault
cd <你的项目>
echo '{"task_id":"init","user_intent":"promptcraft 已初始化"}' \
  | python skills/prompt-memory/scripts/checkpoint.py

# 3. 验证 — 子Agent 通过 .claude/agents/promptcraft.md 自动注册
echo '{"task":"写一个 hello 函数","mode":"build"}' \
  | python promptcraft-agent/subagent_adapter.py
```

子Agent 现已作为 `promptcraft` 在 Claude Code 中可用。可显式调用，
或由 `promptcraft-bridge` 技能在遇到复杂任务时自动触发。

## 执行边界（5层纵深防御）

借鉴 Claude Code 的7层权限系统，为子Agent的真实威胁模型
（**知识污染**而非Shell注入）重新设计：

| 层 | 防护对象 | 硬拒绝触发条件 |
|----|---------|--------------|
| 1 — 输入 | 注入检测、模式一致性 | 系统指令覆盖、模式-协议不匹配 |
| 2 — 工具 | 每工具安全属性 + `check_permissions()` | **MODIFIES_SKILLS**（bypass-immune，永不可绕过） |
| 3 — Vault | 大小上限(8KB)、速率限制(50/会话)、去重、GLOBAL质量≥4 | 超上限、低质量GLOBAL写入 |
| 4 — 输出 | Schema强制、敏感信息扫描、大小限制 | Schema违规、载荷溢出 |
| 5 — 熔断 | 拒绝追踪、3态状态机 | 连续3次拒绝→OPEN(冷却5分钟) |

**核心规则：** 所有工具 `MODIFIES_SKILLS = False`。Skill修改是bypass-immune硬拒绝——
PromptCraft只建议，主Agent执行。

## 项目结构

```
PromptCraft/
├── promptcraft-agent/
│   ├── subagent_adapter.py    # 统一入口，6模式路由
│   ├── engine.py              # 生命周期管理，5个invoke_*方法
│   ├── builder.py             # 单次构建管线（8节提示词）
│   ├── protocol.py            # I/O schema，6个Mode值
│   ├── health_report.py       # HealthReport + 阈值门控
│   ├── context.py             # EngineContext — 3层状态容器
│   ├── boundary.py            # 5层执行边界守卫
│   ├── circuit_breaker.py     # 3态熔断器
│   ├── loop.py                # CLI入口
│   ├── system_prompt.md       # 7层渐进式系统提示词
│   ├── AGENT.md               # Claude Code子Agent定义
│   └── tools/                 # 五引擎工具系统
│       ├── base.py            # 工具基类 + 安全属性
│       ├── personalization.py # Skill叠加注入
│       ├── prompt_build.py    # 完整提示词生成(兜底)
│       ├── feedback_collect.py # 显式+隐式反馈
│       ├── pattern_analysis.py # 聚合模式发现
│       └── skill_advisor.py   # 进化/创建建议
├── skills/
│   ├── prompt-memory/         # 双存储vault I/O + 联邦
│   │   ├── scripts/           #   checkpoint.py + hydrate.py
│   │   └── references/        #   vault schema
│   ├── prompt-techniques/     # 7种技巧参考目录
│   │   └── references/        #   zero-shot 到 tree-of-thought
│   └── promptcraft-bridge/    # 纯触发器Skill → 子Agent委托
│       └── references/        #   启发式触发指南
├── tests/
│   ├── test_scripts.py        # checkpoint, hydrate, federation, freshness
│   ├── test_health_report.py  # 阈值, stall, consistency, proactive
│   ├── test_subagent_adapter.py # 路由, 解析, batch, E2E
│   ├── test_engine_modes.py   # 5个 invoke_* + silent analysis + batch
│   ├── test_integration.py    # 完整闭环工作流
│   └── test_boundary.py       # 5层守卫, 熔断器, 工具, batch输入
├── .claude/agents/            # 子Agent注册
├── CLAUDE.md                  # 项目约定
└── README.md / README.zh-CN.md
```

## 核心特性

- **子Agent架构**：隔离上下文，vault持久化，跨会话改进——通过触发器Skill + vault-hydrate预检按需唤醒
- **批处理**：单次调用处理多任务——hydrate一次，按Skill分组，并行执行（最多4线程）
- **主动信号**：每次响应附带vault感知的上下文提示（类似任务、常见陷阱），不改变被动触发模型
- **5层执行边界**：借鉴Claude Code纵深防御，为子Agent真实威胁模型（知识污染，非Shell注入）重新设计
- **熔断器**：3态状态机（CLOSED → OPEN → HALF_OPEN），拒绝追踪 + 自动冷却
- **多项目联邦**：双层vault——全局(`~/.promptcraft/`) + 项目(`./.promptcraft/`)
- **查询扩展**：同义词查询扩展 + 跨语言（中文→英文）映射，Jaccard检索前自动展开（零依赖）
- **批量反馈持久化**：缓冲的vault写入——反馈记录在内存中累积，批量刷新到vault（NDJSON），降低子进程开销
- **引擎指标**：可观测的静默失败计数器（vault写入错误、子进程超时、分析异常），通过 HealthReport 暴露出退化信号
- **Vault 清理**：`hydrate.py --prune --older-than N` 清理过期条目，GLOBAL条目永不删除，`.md` 文件完整保留
- **执行反馈闭环**：每次执行后结构化质量评分(1-5)写回vault
- **Health Report**：紧凑单行信号——`[PC: N records, action=...]`——告知主Agent何时运行分析
- **Skill-Advisor**：数据支撑的进化/创建建议——绝不自动修改Skill
- **追加式Vault**：完整版本历史，支持回滚，双存储（JSON索引 + Markdown提示词）
- **多文字分词器**：中日韩 + 日文假名 + 韩文 + 拉丁 + 西里尔

## 技术选型

- **仅Python标准库** — 无需pip install、无需venv
- **双存储** — JSON vault（元数据）+ `.md` 文件（完整提示词）
- **双层联邦** — 全局vault + 项目vault，自动合并
- **子Agent模型** — 隔离上下文，触发器式唤醒
- **Jaccard相似度** — 多文字分词器，零外部依赖
- **零外部API** — 无embedding服务，无专有API

## 设计原则

- **增强而非替代** — Skill拥有工作流，PromptCraft提供叠加
- **Fail-closed** — 守卫不确定就拒绝；MODIFIES_SKILLS是bypass-immune
- **仅Health Report** — 内部vault状态绝不暴露给主Agent
- **绝不自动修改Skill** — 仅建议，执行由主Agent负责
- **importance = blast radius** — GLOBAL影响所有项目，升级需数据支撑
- **追加不覆盖** — 完整版本历史保留
- **零外部依赖** — 纯文件系统，人类可读JSON/Markdown

## 许可证

MIT License。详见 [LICENSE](LICENSE)。
