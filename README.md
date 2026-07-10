# Harness AgentOS

Harness AgentOS 是一个用纯 Python 实现的多 Agent 自主执行框架。它把一个自然语言任务拆成可持续运行的工程流程：路由任务类型、规划、构建、验证、记录状态，并在必要时继续下一轮迭代。

项目目标不是封装某个特定 Agent SDK，而是复现并扩展长时间自主开发所需要的关键机制：Profile 分场景策略、工具调用循环、上下文压缩与重置、Skill 渐进式加载、可恢复状态文件、Web 控制台和 Terminal-Bench 风格任务适配。

[English README](README_EN.md)

## 项目背景

本项目受 Anthropic 的长时间应用开发 Harness 架构启发，核心问题是：如何让 LLM 不只是回答问题，而是在较长时间内稳定地规划、执行、测试和修正。

传统的一次性提示很容易遇到几个问题：

- 上下文越来越长，模型开始提前收工或遗忘关键约束。
- 不同任务需要不同工具和验收标准，单一系统提示难以兼顾。
- 运行过程不可恢复，一旦中断就只能重来。
- Agent 是否真的做完，缺少可执行验证和结构化记录。

Harness AgentOS 用几个简单但可组合的模块解决这些问题：

- `Agent` 负责核心 while loop：调用模型、执行工具、把结果放回上下文。
- `Profile` 负责场景差异：Web 应用、终端任务、代码修复、推理问答。
- `Middleware` 负责行为约束：循环检测、退出前验证、时间预算、错误引导。
- `Skill` 负责按需注入领域知识，而不是把所有知识一次性塞进提示词。
- `orchestrator` 负责状态驱动运行，让任务可以暂停、恢复、路由和分析。
- `web` 提供浏览器控制台，用于创建、暂停、恢复任务并查看产物和 trace。

## 项目边界

Harness AgentOS 关注的是 Agent 运行架构，而不是替代完整的研发平台。它适合研究和实践“如何让模型更稳定地执行长任务”，也适合在受控环境里跑 Web 生成、终端任务、代码修复和推理类工作流。

| 范围 | 说明 |
| --- | --- |
| 做什么 | 编排 Agent 循环、Profile 策略、工具调用、状态恢复、验证反馈、记忆和策略提示 |
| 不做什么 | 不提供生产级权限系统、租户隔离、计费系统、任务队列集群或完整 DevOps 平台 |
| 适合场景 | 本地实验、Benchmark 适配、教学复现、受控自动化任务、Agent 架构迭代 |
| 不适合场景 | 直接托管不可信用户任务、无沙箱执行高风险命令、替代人工审核的生产发布流程 |

实际运行时，Agent 会读写文件、执行命令、启动服务并可能调用浏览器测试。建议在隔离工作区、容器或虚拟机中运行高风险任务，并为模型 API、文件系统和网络访问设置清晰边界。

## 技术栈

- 语言：Python 3.10+
- LLM 接口：OpenAI-compatible Chat Completions API
- 依赖：`openai`、`tiktoken`、`playwright`、`fastapi`、`uvicorn`
- Web 控制台：FastAPI + 静态 HTML/CSS/JS
- 浏览器验证：Playwright Chromium
- 状态持久化：JSON state file + 本地 SQLite orchestrator store
- 记忆系统：JSON-backed routing memory + failure-pattern strategy hints
- Benchmark 适配：Harbor / Terminal-Bench 2.0 风格任务
- 测试：Python `unittest`

## 5 分钟快速使用

### 1. 创建环境

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

如果要运行 Web 应用构建和浏览器验收，安装 Chromium：

```bash
python -m playwright install chromium
```

### 2. 配置模型

复制环境变量模板：

```bash
copy .env.template .env
```

编辑 `.env`：

```bash
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
HARNESS_MODEL=gpt-4o
HARNESS_WORKSPACE=./workspace
```

只要服务兼容 OpenAI API，就可以替换 `OPENAI_BASE_URL` 和 `HARNESS_MODEL`。

### 3. 运行一个任务

默认 Profile 是 `app-builder`：

```bash
python harness.py "Build a Pomodoro timer with start, pause, reset buttons. Single HTML file."
```

运行终端任务：

```bash
python harness.py --profile terminal "Fix the broken symlinks in /tmp"
```

查看可用 Profile：

```bash
python harness.py --list-profiles
```

### 4. 启动 Web Console

```bash
python harness.py --ui --port 8765
```

然后打开：

```text
http://127.0.0.1:8765
```

Web Console 支持创建任务、自动路由 Profile、暂停/恢复运行、批准需要人工确认的步骤、查看 trace 和工作区产物。

### 5. 恢复中断的任务

每次状态驱动运行都会在工作区写入 `harness_state.json`。可以用 run id、state 文件路径或工作区路径恢复：

```bash
python harness.py --resume 20260710-153000
python harness.py --resume workspace/20260710-153000/harness_state.json
python harness.py --resume workspace/20260710-153000
```

## 常用命令

```bash
# Web 应用生成
python harness.py --profile app-builder "Build an interactive periodic table with search and filters"

# Terminal-Bench 风格任务
python harness.py --profile terminal "Set up a QEMU VM with Alpine Linux and SSH access"

# SWE-Bench 风格代码修复
python harness.py --profile swe-bench "Fix the TypeError in parse_config()"

# 纯推理任务
python harness.py --profile reasoning "Calculate the escape velocity of Mars"

# 启动控制台
python harness.py --ui --port 8765
```

## 项目架构

```text
用户任务
  |
  v
Router / Profile 选择
  |
  v
Planner -> spec.md
  |
  v
Contract negotiation -> contract.md  (app-builder 启用)
  |
  v
Builder -> 代码、脚本、产物
  |
  v
Evaluator -> feedback.md、score
  |
  v
Analyze -> analysis.json、memory update
  |
  +-- 分数未达标时进入下一轮 Build/Evaluate
```

CLI 的传统路径由 `harness.py` 直接编排。Web Console 和 `--resume` 使用 `orchestrator.scheduler.Scheduler`，以 `harness_state.json` 为唯一事实来源逐步推进任务。

### 核心模块

| 模块 | 职责 |
| --- | --- |
| `harness.py` | CLI 入口；启动 UI；传统 Plan/Build/Evaluate 循环；恢复状态驱动任务 |
| `agents.py` | 单个 Agent 的核心循环：模型调用、工具调用、上下文生命周期、trace 写入 |
| `tools.py` | 文件、命令、浏览器测试、Skill 读取、子 Agent 委派等工具实现和 schema |
| `context.py` | token 估算、上下文压缩、焦虑信号检测、checkpoint/reset |
| `profiles/` | 不同任务场景的 Agent prompt、工具集合、评分阈值、时间预算 |
| `middlewares.py` | 循环检测、退出前验证、时间预算、骨架代码检测、错误指导 |
| `skills.py` / `skills/` | Skill 注册和任务领域知识目录 |
| `orchestrator/` | 状态机、路由、记忆、策略提示、分析、hook、路径安全和可恢复运行 |
| `web/` | FastAPI 控制台和浏览器端 UI |
| `benchmarks/` | Harbor / Terminal-Bench 适配 |
| `tests/` | Profile、Scheduler、Router、Web Server、Memory 等单元测试 |

## Profile 系统

Profile 是项目的任务策略层。它决定一个任务使用哪些 Agent、哪些工具、是否需要 evaluator、如何提取分数，以及每个阶段的时间预算。

| Profile | 用途 | 特点 |
| --- | --- | --- |
| `app-builder` | 从一句话生成 Web 应用 | 启用 Planner、Builder、Evaluator 和合同协商；Evaluator 可用 Playwright |
| `terminal` | 终端 / CLI / Terminal-Bench 风格任务 | 偏单 Agent 执行；强调命令验证、时间预算和退出前检查 |
| `swe-bench` | 代码修复任务 | 面向仓库 bug 修复和测试验证 |
| `reasoning` | 推理问答 | 适合数学、逻辑、解释类任务 |

Profile 支持环境变量覆盖，格式为：

```bash
PROFILE_<PROFILE_NAME>_<KEY>=value
```

示例：

```bash
PROFILE_TERMINAL_PASS_THRESHOLD=9.0
PROFILE_TERMINAL_MAX_ROUNDS=3
PROFILE_TERMINAL_TASK_BUDGET=1800
```

## 状态驱动编排

`orchestrator` 是 AgentOS 相比普通脚本模式更重要的一层。它把运行拆成离散 action，并把每一步写入状态文件。

状态文件位置：

```text
workspace/<run_id>/harness_state.json
```

典型字段包括：

- `prompt`：原始任务
- `profile`：当前选择的 Profile，可能先是 `auto`
- `phase` / `next_action`：当前阶段和下一步动作
- `round_num`：当前迭代轮次
- `score_history`：评价分数历史
- `artifacts`：产物索引
- `events`：最近状态事件
- `active` / `status`：是否继续运行和当前状态

状态驱动的好处：

- 进程中断后可以恢复。
- Web Console 可以实时读取状态，不依赖 Python 内存对象。
- 任务路由、人工确认、hook、分析和记忆更新都有明确边界。
- 每个 workspace 都保留 trace、反馈和分析结果，便于复盘。

Hook 系统位于 `orchestrator/hooks.py`，负责阶段超时、心跳、预算、人工批准、验证结果、产物变化和记忆更新前后的事件记录。它不会替代核心 Agent 循环，而是在 Scheduler 阶段边界执行策略检查。

## 记忆系统

记忆系统是项目的历史经验层。它记录粗粒度运行结果，用来微调自动路由和生成低优先级策略提示；不会保存完整 Agent 上下文，也不会覆盖用户手动选择的 Profile。

| 层级 | 字段 | 作用 |
| --- | --- | --- |
| 短期记忆 | `short_term.runs` | 保留最近运行，默认最多 50 条，用来影响同类任务的路由置信度 |
| 长期记忆 | `long_term.task_types` / `long_term.global_profiles` | 按任务类型和 Profile 聚合成功率、平均分、工具调用数和失败原因 |
| 策略提示 | `strategy_hints` | 根据重复失败生成 advisory-only 提示，注入 builder/evaluator 阶段 |

记忆在状态驱动任务的 `analyze` 阶段更新，来源包括 trace、分数、错误、产物分析和最终状态。敏感预览会做基础脱敏，例如包含 `api_key`、`authorization`、`password`、`secret`、`token` 时写入 `[redacted]`。

默认记忆文件是 `.harness_memory.json`。可以通过环境变量指定其他位置：

```bash
HARNESS_MEMORY_FILE=./workspace/harness-memory.json
```

如果需要重置历史偏置，停止正在运行的任务后删除记忆文件即可；这不会影响已有 workspace、trace 或 `harness_state.json`。

## Skill 机制

Skill 目录位于 `skills/`。每个 Skill 是一个带 YAML frontmatter 的 `SKILL.md`，描述某类任务的专门解法。

基本结构：

```text
skills/
  my-skill/
    SKILL.md
```

示例 frontmatter：

```markdown
---
name: my-skill
description: One-line description used by the agent to decide when to load it.
---
```

本项目支持两种 Skill 使用方式：

- 通用 Agent 可通过工具按需读取 `SKILL.md`，实现渐进式披露。
- `terminal` Profile 会根据任务名或工作区路径自动匹配一个最相关 Skill，并注入到 builder 任务中，减少上下文浪费。

## Web Console

启动：

```bash
python harness.py --ui --port 8765
```

主要 API：

| API | 用途 |
| --- | --- |
| `POST /api/runs` | 创建运行 |
| `GET /api/runs` | 列出运行 |
| `GET /api/runs/{run_id}` | 查看状态 |
| `POST /api/runs/{run_id}/pause` | 暂停 |
| `POST /api/runs/{run_id}/resume` | 恢复 |
| `POST /api/runs/{run_id}/approve` | 批准需要人工确认的动作 |
| `GET /api/runs/{run_id}/events` | Server-Sent Events 状态流 |
| `POST /api/terminal/run` | 在控制台终端执行命令 |

## 工作区产物

默认输出目录是 `workspace/`。每次普通 CLI 运行会创建一个带时间戳的子目录；状态驱动运行以 `run_id` 为目录名。

常见产物：

```text
spec.md                 任务规格或规划
contract.md             本轮验收合同，app-builder 使用
feedback.md             evaluator 输出
progress.md             运行进度记录
analysis.json           trace 和产物分析
harness_state.json      状态驱动运行的状态文件
_trace_<agent>.jsonl    每个 Agent 的结构化事件 trace
.harness/orchestrator.db 当前 workspace 的状态快照和事件索引
```

跨运行的默认记忆文件位于项目根目录：

```text
.harness_memory.json
```

## 配置项

基础配置来自 `.env` 或环境变量：

```bash
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
HARNESS_MODEL=gpt-4o
HARNESS_WORKSPACE=./workspace
HARNESS_MEMORY_FILE=./.harness_memory.json
```

可选调参：

```bash
MAX_HARNESS_ROUNDS=5
PASS_THRESHOLD=7.0
COMPRESS_THRESHOLD=50000
RESET_THRESHOLD=100000
MAX_AGENT_ITERATIONS=500
ENABLE_PARALLEL_TOOL_CALLS=0
```

说明：

- `COMPRESS_THRESHOLD` 达到后会压缩旧上下文。
- `RESET_THRESHOLD` 达到后会通过 checkpoint 重建上下文。
- `ENABLE_PARALLEL_TOOL_CALLS` 默认关闭，因为部分模型并行工具调用稳定性较差。

## Terminal-Bench / Harbor

项目包含 `benchmarks/harbor_agent.py` 和 `run_benchmark_v2.sh`，用于 Harbor / Terminal-Bench 风格评测。

安装 Harbor 后可运行单任务：

```bash
pip install harbor
harbor run -d "terminal-bench@2.0" ^
  --agent-import-path benchmarks.harbor_agent:HarnessAgent ^
  --task-names hello-world
```

在类 Unix shell 中可使用：

```bash
./run_benchmark_v2.sh
```

## 运行测试

项目使用 `unittest`：

```bash
python -m unittest discover -s tests
```

如果只想跑 Web Console 相关测试：

```bash
python -m unittest tests.test_web_server
```

部分功能依赖外部环境：

- `app-builder` 的浏览器验收需要 Playwright Chromium。
- 实际 Agent 运行需要可用的 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和模型。
- Harbor benchmark 需要额外安装 Harbor，并准备对应任务环境。

## 开发约定

- 优先保持改动小而明确，避免无关重构。
- 修改已有逻辑时，先确认当前行为和兼容边界。
- 新增 Profile 时，把场景策略放在 `profiles/`，不要塞进 `harness.py`。
- 新增领域知识时，优先放到 `skills/<name>/SKILL.md`。
- 新增状态驱动能力时，优先通过 `orchestrator` 的 state、scheduler、hook 扩展。
- 改完后至少运行相关单元测试；涉及 Web/UI 时同时做手动冒烟验证。

## License

MIT
