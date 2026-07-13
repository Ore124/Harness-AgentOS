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
- 失败恢复：Evidence-Guided Recovery，用结构化失败证据指导下一轮 targeted repair
- Benchmark 适配：Harbor / Terminal-Bench 2.0 风格任务
- 测试：Python `unittest`

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
HARNESS_EVIDENCE_GUIDED_RECOVERY=1
HARNESS_METRICS_ENABLED=1
HARNESS_WEB_TERMINAL_ENABLED=0
HARNESS_WEB_TERMINAL_TOKEN=
```

说明：

- `COMPRESS_THRESHOLD` 达到后会压缩旧上下文。
- `RESET_THRESHOLD` 达到后会通过 checkpoint 重建上下文。
- `ENABLE_PARALLEL_TOOL_CALLS` 默认关闭，因为部分模型并行工具调用稳定性较差。
- `HARNESS_EVIDENCE_GUIDED_RECOVERY` 默认开启；设为 `0` 可回到只依赖普通 feedback 的重试方式。
- `HARNESS_METRICS_ENABLED` 默认开启，指标写入当前 run 的 `.harness/metrics.json`。
- `HARNESS_WEB_TERMINAL_ENABLED` 默认关闭；启用 `/api/terminal/run` 时建议同时设置 `HARNESS_WEB_TERMINAL_TOKEN`。

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
| `orchestrator/` | 状态机、路由、记忆、策略提示、失败证据提取、分析、hook、路径安全和可恢复运行 |
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
- `current_failure_evidence` / `failure_evidence_history`：最近失败证据和同签名失败历史
- `recovery`：失败次数、恢复尝试、重复失败和升级计数
- `artifacts`：产物索引
- `events`：最近状态事件
- `active` / `status`：是否继续运行和当前状态

状态驱动的好处：

- 进程中断后可以恢复。
- Web Console 可以实时读取状态，不依赖 Python 内存对象。
- 任务路由、人工确认、hook、分析和记忆更新都有明确边界。
- 每个 workspace 都保留 trace、反馈和分析结果，便于复盘。

状态驱动 run 的最终状态由实际验收结果决定：有 evaluator 的 Profile 需要最新分数达到 `PASS_THRESHOLD`；带验证结果的任务还要求验证状态为 `verified`。未达到验收条件时，run 会以 `error`/`task_success=false` 结束，而不是仅因为流程走完就标记为成功。

Hook 系统位于 `orchestrator/hooks.py`，负责阶段超时、心跳、预算、人工批准、验证结果、产物变化和记忆更新前后的事件记录。它不会替代核心 Agent 循环，而是在 Scheduler 阶段边界执行策略检查。

### 运行隔离与离线回放

状态驱动运行会为每个 run 安装独立的 `run_id` 和 workspace。Agent 启动的后台命令、dev server 和长时间命令会登记到当前 run，结束、失败或超时时只清理这个 run 自己创建的受管进程，避免误伤 Harness、benchmark runner、其他 run 或外部已有服务。

进程清理按平台处理：Windows 使用受管 PID 的进程树清理，POSIX 使用独立 session/process group。命令 safeguard 会阻止全局终止 Python 的危险命令，例如 `taskkill /im python.exe`、`pkill -f python`、`killall python`。

每个状态驱动 run 还会写入 `.harness/canonical_trace.jsonl`。它记录 Scheduler、LLM、Tool、Safeguard、workspace mutation 和 managed process 生命周期事件，可通过 `replay_trace()` 纯离线回放，不重新调用 LLM 或执行工具。

离线回放会按 role/phase 汇总 calls、tokens 和状态变化，并校验终止事件、`task_success`、ID/序列、LLM/tool call ledger 和 token conservation。`compare_replays()` 可比较 baseline/candidate；不可比较或不完整的 trace 会给出明确原因。

可以用 Fast Regression Gate 对 baseline/candidate 的 canonical trace 做离线回归检查：

```bash
python scripts/run_fast_regression_gate.py ^
  --baseline workspace/baseline/.harness/canonical_trace.jsonl ^
  --candidate workspace/candidate/.harness/canonical_trace.jsonl
```

Gate 只读取 trace，不执行工具或调用模型。结果为 `PASS`、`FAIL` 或 `INCONCLUSIVE`：它会先检查 trace 是否完整、样本是否可比较，再比较成功率、安全事件和每次成功的 tokens、LLM calls、agent rounds、wall time 等指标。

核心文件：`orchestrator/run_context.py`、`tools.py`、`orchestrator/scheduler.py`、`orchestrator/canonical_trace.py`、`orchestrator/regression_gate.py`。

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

## Evidence-Guided Recovery

当 Builder、Evaluator 或验证阶段失败时，Harness AgentOS 默认启用 Evidence-Guided Recovery。它不会新增 Agent，也不会增加额外 LLM 调用，而是从已有 `feedback.md`、trace、异常信息和 git diff 中程序化提取结构化失败证据，并在下一轮重试时给 Builder 提供更聚焦的修复上下文。

结构化证据包含 `failure_type`、稳定的 `failure_signature`、失败检查项、关键错误片段、疑似相关文件、最近变更文件、`retry_goal`、`same_failure_count` 和 `recovery_strategy`。同一失败第一次使用 `targeted_fix`，第二次升级为 `reinspect_assumptions`，第三次及以上升级为 `escalate_analysis`，避免反复执行同一种无效修复路径。

该功能默认开启，可通过环境变量关闭：

```bash
HARNESS_EVIDENCE_GUIDED_RECOVERY=0
```

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
.harness/metrics.json   token、round、recovery 和 benchmark 汇总指标
.harness/canonical_trace.jsonl 版本化的运行生命周期、LLM、工具和进程事件
```

跨运行的默认记忆文件位于项目根目录：

```text
.harness_memory.json
```


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

Evidence-Guided Recovery 也提供本地确定性恢复场景 benchmark，用于比较 baseline 和结构化失败证据重试策略：

```bash
python benchmarks/run_evidence_recovery_benchmark.py
```

报告会写入 `benchmark_runs/evidence_recovery_report.json`。

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

## 致谢

感谢 [lazyFrogLOL/Harness_Engineering](https://github.com/lazyFrogLOL/Harness_Engineering.git) 仓库对本项目的启发和参考。
