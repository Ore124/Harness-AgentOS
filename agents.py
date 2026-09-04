"""
Agent implementation — the core while loop with tool use.
Uses OpenAI-compatible chat completions API with function calling.
"""
from __future__ import annotations

import json
import uuid
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from openai import OpenAI

import config
import tools
from orchestrator.canonical_trace import emit_event
import context
import metrics

log = logging.getLogger("harness")

EVALUATOR_FINALIZATION_RESERVE_SECONDS = 15
EVALUATOR_FINALIZATION_TOOLS = {
    "read_file",
    "write_file",
    "list_files",
    "stop_dev_server",
}


# ---------------------------------------------------------------------------
# Trace writer — records every agent event to a JSONL file
# ---------------------------------------------------------------------------

class TraceWriter:
    """Appends structured events to a JSONL trace file in the workspace.

    Each line is a JSON object with: timestamp, agent, event_type, and data.
    Trace file: {WORKSPACE}/_trace_{agent_name}.jsonl
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self._start_time = time.time()
        # Write trace to workspace first; fall back to harness-agent dir
        trace_dir = tools.current_workspace()
        try:
            trace_dir.mkdir(parents=True, exist_ok=True)
            test_file = trace_dir / f"_trace_test_{agent_name}"
            test_file.write_text("test")
            test_file.unlink()
            self._path = trace_dir / f"_trace_{agent_name}.jsonl"
        except Exception:
            # Workspace not writable, use harness-agent dir
            self._path = Path(__file__).parent / f"_trace_{agent_name}.jsonl"

    def _write(self, event_type: str, data: dict):
        started = time.perf_counter()
        try:
            entry = {
                "t": round(time.time() - self._start_time, 2),
                "agent": self.agent_name,
                "event": event_type,
                **data,
            }
            line = json.dumps(entry, ensure_ascii=False)[:10000]
            # Write to file
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            # Also print to stderr so Harbor logs capture it
            import sys
            print(f"[TRACE] {line}", file=sys.stderr)
        except Exception:
            pass  # never let tracing break the agent
        finally:
            metrics.RECORDER.add_latency("trace_log_persistence_ms", int((time.perf_counter() - started) * 1000))

    def iteration(self, n: int, tokens: int):
        self._write("iteration", {"n": n, "tokens": tokens})

    def llm_response(self, content: str | None, tool_calls: list | None, finish_reason: str | None):
        self._write("llm_response", {
            "content": (content or "")[:500],
            "tool_calls": [tc["function"]["name"] for tc in (tool_calls or [])],
            "finish_reason": finish_reason,
        })

    def tool_call(self, name: str, args: dict, result: str):
        outcome = tools.classify_tool_result(result, name)
        self._write("tool_call", {
            "tool": name,
            "args": _truncate(json.dumps(args, ensure_ascii=False), 300),
            "result": _truncate(result, 500),
            "success": outcome.success,
            "failure_kind": outcome.failure_kind,
            "exit_code": outcome.exit_code,
        })

    def middleware_inject(self, source: str, hook: str, message: str):
        metrics.RECORDER.record_middleware_injection(source, hook, message)
        self._write("middleware", {
            "source": source,
            "hook": hook,
            "message": message[:300],
        })

    def context_event(self, event_type: str, reason: str = ""):
        self._write("context", {"type": event_type, "reason": reason})

    def error(self, error_type: str, message: str):
        self._write("error", {"type": error_type, "message": message[:500]})

    def finish(self, reason: str, iterations: int):
        self._write("finish", {"reason": reason, "iterations": iterations})

# ---------------------------------------------------------------------------
# LLM client (singleton)
# ---------------------------------------------------------------------------

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.API_KEY,
            base_url=config.BASE_URL,
            timeout=300.0,        # 5 min per request
            max_retries=2,
        )
    return _client


def llm_call_simple(messages: list[dict]) -> str:
    """Simple LLM call without tools — used for summarization.
    Retries on rate limits to avoid crashing the agent during context compaction."""
    import random
    for attempt in range(4):
        try:
            started = time.perf_counter()
            resp = get_client().chat.completions.create(
                model=config.MODEL,
                messages=messages,
                max_tokens=10000,
            )
            metrics.RECORDER.record_llm_call(
                role="context_summarizer",
                phase=None,
                model=config.MODEL,
                latency_ms=int((time.perf_counter() - started) * 1000),
                usage=getattr(resp, "usage", None),
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            err_str = str(e)
            if ("rate_limit" in err_str.lower() or "429" in err_str) and attempt < 3:
                wait = min(2 ** (attempt + 1), 30) + random.uniform(0, 3)
                log.warning(f"llm_call_simple rate limited, waiting {wait:.1f}s (attempt {attempt+1}/4)")
                metrics.RECORDER.add_latency("explicit_sleep_polling_ms", int(wait * 1000))
                time.sleep(wait)
                continue
            log.error(f"llm_call_simple failed: {e}")
            # Return a minimal summary rather than crashing
            return "[context summarization failed — continuing with truncated context]"
    return "[context summarization failed after retries]"


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRunResult:
    """The terminal outcome of one managed agent loop."""

    text: str
    exit_reason: str
    iterations: int

    @property
    def succeeded(self) -> bool:
        """Whether the model ended the loop normally without tool calls."""
        return self.exit_reason == "no_tool_calls"

class Agent:
    """
    A single agent with a system prompt and tool access.

    This is the 'managed agent loop' from the architecture:
    - while loop with llm.call(prompt)
    - tool execution
    - context lifecycle (compaction / reset)

    Skills are handled via progressive disclosure:
    - Level 1: skill catalog (name + description) is baked into system_prompt
    - Level 2: agent decides to read_skill_file("skills/.../SKILL.md") on its own
    - Level 3: SKILL.md references sub-files, agent reads those too
    No external code decides which skills to load — the agent does.
    """

    def __init__(self, name: str, system_prompt: str, use_tools: bool = True,
                 extra_tool_schemas: list[dict] | None = None,
                 tool_schemas: list[dict] | None = None,
                 middlewares: list | None = None,
                 time_budget: float | None = None):
        self.name = name
        self.system_prompt = system_prompt
        self.use_tools = use_tools
        self.extra_tool_schemas = extra_tool_schemas or []
        self.tool_schemas = tool_schemas  # None = use default TOOL_SCHEMAS
        self.middlewares = middlewares or []  # list[AgentMiddleware]
        self.time_budget = time_budget

    def run(
        self,
        task: str,
        *,
        run_id: str | None = None,
        phase: str | None = None,
        run_context=None,
    ) -> AgentRunResult:
        """Run the agent with an optional per-run workspace context."""
        if run_context is None:
            return self._run(task, run_id=run_id, phase=phase)
        with run_context.activate():
            return self._run(task, run_id=run_id, phase=phase)

    def _run(self, task: str, *, run_id: str | None = None, phase: str | None = None) -> AgentRunResult:
        """
        Execute the agent loop until the model stops calling tools
        or we hit the iteration limit.

        Returns the final assistant text and terminal exit reason.
        Writes a JSONL trace file to {WORKSPACE}/_trace_{name}.jsonl
        """
        trace = TraceWriter(self.name)
        if run_id:
            metrics.RECORDER.start_run(run_id, tools.current_workspace())

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt, "_metrics_category": "static_system_prompt"},
            {"role": "user", "content": task, "_metrics_category": "dynamic_task_state"},
        ]

        client = get_client()
        consecutive_errors = 0
        last_text = ""
        agent_started = time.time()
        exit_reason = "max_iterations"
        iterations = 0

        for iteration in range(1, config.MAX_AGENT_ITERATIONS + 1):
            iterations = iteration
            elapsed = time.time() - agent_started
            if self.time_budget is not None and elapsed >= self.time_budget:
                log.warning(f"[{self.name}] Time budget exceeded ({self.time_budget}s).")
                trace.finish("time_budget", iteration)
                exit_reason = "time_budget"
                break

            finalizing_for_time = self._should_finalize_for_time(elapsed)
            if finalizing_for_time and not any(
                m.get("_metrics_category") == "time_budget_finalization"
                for m in messages
            ):
                inject = (
                    "[SYSTEM] Evaluator time is nearly exhausted. Stop browser testing and "
                    "other expensive checks now. Use the evidence already collected, call "
                    "stop_dev_server if needed, write feedback.md immediately, then finish."
                )
                messages.append({
                    "role": "user",
                    "content": inject,
                    "_metrics_category": "time_budget_finalization",
                })
                trace.middleware_inject("agent_loop", "time_budget_finalization", inject)

            # --- Middleware: per-iteration hooks ---
            middleware_started = time.perf_counter()
            for mw in self.middlewares:
                inject = mw.per_iteration(iteration, messages)
                if inject:
                    messages.append({"role": "user", "content": inject, "_metrics_category": "middleware_injections"})
                    trace.middleware_inject(type(mw).__name__, "per_iteration", inject)
            metrics.RECORDER.add_latency("middleware_ms", int((time.perf_counter() - middleware_started) * 1000))

            # --- Context lifecycle check ---
            context_started = time.perf_counter()
            token_count = context.count_tokens(messages)
            log.info(f"[{self.name}] iteration={iteration}  tokens≈{token_count}")
            trace.iteration(iteration, token_count)
            _canonical_emit(run_id, "agent_round", {"iteration": iteration}, role=self.name, phase=phase)
            metrics.RECORDER.record_agent_round(self.name, phase, iteration)
            metrics.RECORDER.add_latency("context_processing_ms", int((time.perf_counter() - context_started) * 1000))

            if token_count > config.RESET_THRESHOLD or context.detect_anxiety(messages):
                reason = "anxiety detected" if token_count <= config.RESET_THRESHOLD else f"tokens {token_count} > threshold"
                log.warning(f"[{self.name}] Context reset triggered ({reason}). Writing checkpoint...")
                trace.context_event("reset", reason)
                metrics.RECORDER.record_context_event("reset")
                compression_started = time.perf_counter()
                checkpoint = context.create_checkpoint(messages, llm_call_simple)
                messages = context.restore_from_checkpoint(checkpoint, self.system_prompt)
                _mark_unattributed_messages(messages, "compression_reset")
                metrics.RECORDER.add_latency("compression_reset_ms", int((time.perf_counter() - compression_started) * 1000))
            elif token_count > config.COMPRESS_THRESHOLD:
                log.info(f"[{self.name}] Compacting context (role={self.name})...")
                trace.context_event("compact", f"tokens={token_count}")
                metrics.RECORDER.record_context_event("compact")
                compression_started = time.perf_counter()
                messages = context.compact_messages(messages, llm_call_simple, role=self.name)
                _mark_unattributed_messages(messages, "compression_reset")
                metrics.RECORDER.add_latency("compression_reset_ms", int((time.perf_counter() - compression_started) * 1000))

            # --- LLM call ---
            kwargs = dict(
                model=config.MODEL,
                messages=_messages_for_api(messages),
                max_tokens=8192,
            )
            if self.use_tools:
                base_schemas = self.tool_schemas if self.tool_schemas is not None else tools.TOOL_SCHEMAS
                tool_schemas = base_schemas + self.extra_tool_schemas
                if finalizing_for_time:
                    tool_schemas = _filter_tool_schemas(tool_schemas, EVALUATOR_FINALIZATION_TOOLS)
                kwargs["tools"] = tool_schemas
                kwargs["tool_choice"] = "auto"
                # Parallel tool calls: only enable for models known to handle it well.
                # Weaker models produce malformed parallel calls that waste time.
                # Controlled via config; default OFF for safety.
                if config.ENABLE_PARALLEL_TOOL_CALLS:
                    kwargs["parallel_tool_calls"] = True
            metrics.RECORDER.record_token_attribution(
                role=self.name,
                phase=phase,
                categories=_categorize_messages_for_metrics(messages),
                estimated_input_tokens=token_count,
            )

            try:
                llm_started = time.perf_counter()
                llm_call_id = uuid.uuid4().hex
                _canonical_emit(run_id, "llm_request_started", {"llm_call_id": llm_call_id}, role=self.name, phase=phase)
                response = client.chat.completions.create(**kwargs)
                llm_latency_ms = int((time.perf_counter() - llm_started) * 1000)
                llm_call_index = metrics.RECORDER.record_llm_call(
                    role=self.name,
                    phase=phase,
                    model=config.MODEL,
                    latency_ms=llm_latency_ms,
                    usage=getattr(response, "usage", None),
                )
                usage = getattr(response, "usage", None)
                _canonical_emit(run_id, "llm_request_completed", {
                    "llm_call_id": llm_call_id,
                    "input_tokens": _usage_value(usage, "prompt_tokens", "input_tokens"),
                    "cached_tokens": _usage_nested_value(usage, "prompt_tokens_details", "cached_tokens"),
                    "output_tokens": _usage_value(usage, "completion_tokens", "output_tokens"),
                    "latency_ms": llm_latency_ms,
                }, role=self.name, phase=phase)
            except Exception as e:
                _canonical_emit(run_id, "llm_request_failed", {"llm_call_id": locals().get("llm_call_id", uuid.uuid4().hex), "latency_ms": int((time.perf_counter() - llm_started) * 1000) if "llm_started" in locals() else 0, "error": str(e)}, role=self.name, phase=phase)
                err_str = str(e)
                trace.error("api_error", err_str)

                # Rate limits get longer backoff and don't count toward abort threshold
                if "rate_limit" in err_str.lower() or "429" in err_str:
                    import random
                    wait = min(2 ** (consecutive_errors + 2), 120) + random.uniform(0, 5)
                    log.warning(f"[{self.name}] Rate limited, waiting {wait:.1f}s...")
                    metrics.RECORDER.add_latency("explicit_sleep_polling_ms", int(wait * 1000))
                    time.sleep(wait)
                    # Don't increment consecutive_errors — rate limits are transient
                    continue

                # JSON parse failures (common with weak/quantized models generating
                # long tool call arguments that get truncated mid-string).
                # The inference server returns 500 because the JSON is incomplete.
                # Don't count toward abort threshold — nudge the model to split work.
                err_lower = err_str.lower()
                if ("parse" in err_lower and "json" in err_lower) or \
                   ("invalid" in err_lower and ("string" in err_lower or "json" in err_lower)):
                    log.warning(f"[{self.name}] 🔧 JSON parse error from server — nudging model to split output")
                    trace.error("json_parse_recovery", err_str[:300])
                    messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] Your last tool call FAILED because the arguments were too long "
                            "and the JSON was truncated mid-string. The server could not parse it.\n\n"
                            "YOU MUST split large files into smaller parts:\n"
                            "1. Write the HTML structure first (no inline CSS/JS beyond basics)\n"
                            "2. Write CSS in a separate .css file\n"
                            "3. Write JS in a separate .js file\n"
                            "4. Or use multiple write_file calls for sections of the same file, "
                            "using edit_file to append content after the initial skeleton.\n\n"
                            "NEVER put an entire application in a single write_file call. "
                            "Keep each write_file content under 200 lines."
                        ),
                        "_metrics_category": "other",
                    })
                    metrics.RECORDER.add_latency("explicit_sleep_polling_ms", 1000)
                    time.sleep(1)
                    continue

                log.error(f"[{self.name}] API error: {e}")
                consecutive_errors += 1
                if consecutive_errors >= config.MAX_TOOL_ERRORS:
                    log.error(f"[{self.name}] Too many API errors, aborting.")
                    trace.finish("api_errors", iteration)
                    exit_reason = "api_errors"
                    break
                sleep_seconds = 2 ** consecutive_errors
                metrics.RECORDER.add_latency("explicit_sleep_polling_ms", int(sleep_seconds * 1000))
                time.sleep(sleep_seconds)
                continue

            consecutive_errors = 0

            # --- Guard against empty choices ---
            if not response.choices:
                log.warning(f"[{self.name}] API returned empty choices. Retrying...")
                trace.error("empty_choices", "API returned no choices")
                consecutive_errors += 1
                if consecutive_errors >= config.MAX_TOOL_ERRORS:
                    log.error(f"[{self.name}] Too many empty responses, aborting.")
                    trace.finish("empty_choices", iteration)
                    exit_reason = "empty_choices"
                    break
                metrics.RECORDER.add_latency("explicit_sleep_polling_ms", 2000)
                time.sleep(2)
                continue

            choice = response.choices[0]
            msg = choice.message

            # --- Append assistant message to history ---
            assistant_msg = {"role": "assistant", "content": msg.content, "_metrics_category": "assistant_history"}
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_msg)
            metrics.RECORDER.record_llm_result(
                call_index=llm_call_index,
                tool_names=[tc.function.name for tc in (msg.tool_calls or [])],
                finish_reason=choice.finish_reason,
                content=msg.content,
            )

            # --- Trace the LLM response ---
            trace.llm_response(msg.content, assistant_msg.get("tool_calls"), choice.finish_reason)

            # --- If model produced text, capture it ---
            if msg.content:
                last_text = msg.content
                log.info(f"[{self.name}] assistant: {msg.content[:200]}...")

            # --- If no tool calls, check pre-exit middlewares ---
            if not msg.tool_calls:
                # Detect "text-only" responses where model describes actions
                # instead of executing them — common with weaker models
                if msg.content and iteration <= 5:
                    content_lower = msg.content.lower()
                    action_words = ["i will", "i'll", "let me", "first,", "step 1",
                                    "here's my plan", "i need to", "we need to",
                                    "the approach", "my strategy", "i can",
                                    "we can", "let's", "i would", "i should"]
                    is_planning_text = any(w in content_lower for w in action_words)
                    has_no_prior_tools = not any(
                        m.get("role") == "tool" for m in messages
                    )
                    if is_planning_text and has_no_prior_tools:
                        log.warning(f"[{self.name}] Model is describing instead of executing. Nudging.")
                        trace.middleware_inject("agent_loop", "text_only_nudge",
                                               "Model describing instead of executing")
                        messages.append({
                            "role": "user",
                            "content": (
                                "[SYSTEM] STOP TALKING. USE TOOLS NOW.\n"
                                "Call run_bash or write_file immediately. No more text."
                            ),
                            "_metrics_category": "middleware_injections",
                        })
                        continue

                forced_continue = False
                middleware_started = time.perf_counter()
                for mw in self.middlewares:
                    inject = mw.pre_exit(messages)
                    if inject:
                        messages.append({"role": "user", "content": inject, "_metrics_category": "middleware_injections"})
                        trace.middleware_inject(type(mw).__name__, "pre_exit", inject)
                        forced_continue = True
                        break
                metrics.RECORDER.add_latency("middleware_ms", int((time.perf_counter() - middleware_started) * 1000))
                if forced_continue:
                    continue
                log.info(f"[{self.name}] Finished (no more tool calls).")
                trace.finish("no_tool_calls", iteration)
                exit_reason = "no_tool_calls"
                break

            # --- Execute tool calls ---
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    log.warning(f"[{self.name}] Bad JSON in tool call {fn_name}: {tc.function.arguments[:200]}")
                    trace.error("bad_json", f"{fn_name}: {tc.function.arguments[:200]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"[error] Invalid JSON arguments: {tc.function.arguments[:200]}",
                        "_metrics_category": "tool_results",
                    })
                    continue

                log.info(f"[{self.name}] tool: {fn_name}({_truncate(str(fn_args), 120)})")
                tool_call_id = str(tc.id)
                _canonical_emit(run_id, "tool_requested", {"tool_call_id": tool_call_id, "tool": fn_name, "command": fn_args.get("command"), "path": fn_args.get("path")}, role=self.name, phase=phase)
                blocked = fn_name == "run_bash" and bool(tools._unsafe_process_kill_error(str(fn_args.get("command", ""))))
                _canonical_emit(run_id, "safeguard_blocked" if blocked else "safeguard_allowed", {"tool_call_id": tool_call_id, "tool": fn_name}, role=self.name, phase=phase)
                _canonical_emit(run_id, "tool_started", {"tool_call_id": tool_call_id, "tool": fn_name}, role=self.name, phase=phase)
                tool_started = time.perf_counter()
                result = tools.execute_tool(fn_name, fn_args)
                outcome = tools.classify_tool_result(result, fn_name)
                tool_payload = {
                    "tool_call_id": tool_call_id,
                    "tool": fn_name,
                    "latency_ms": int((time.perf_counter() - tool_started) * 1000),
                    "success": outcome.success,
                    "failure_kind": outcome.failure_kind,
                    "exit_code": outcome.exit_code,
                }
                _canonical_emit(run_id, "tool_completed" if outcome.success else "tool_failed", tool_payload, role=self.name, phase=phase)
                metrics.RECORDER.record_tool_call(
                    role=self.name,
                    phase=phase,
                    tool_name=fn_name,
                    arguments=fn_args,
                    latency_ms=int((time.perf_counter() - tool_started) * 1000),
                    result=result,
                )
                log.debug(f"[{self.name}] tool result: {_truncate(result, 200)}")
                trace.tool_call(fn_name, fn_args, result)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                    "_metrics_category": "tool_results",
                })

                # --- Middleware: post-tool hooks ---
                middleware_started = time.perf_counter()
                for mw in self.middlewares:
                    inject = mw.post_tool(fn_name, fn_args, result, messages)
                    if inject:
                        # For parallel tool calls, only inject AFTER the last tool
                        # to avoid breaking the tool_call/tool_result sequence
                        if tc == msg.tool_calls[-1]:
                            messages.append({"role": "user", "content": inject, "_metrics_category": "middleware_injections"})
                            trace.middleware_inject(type(mw).__name__, "post_tool", inject)
                        break
                metrics.RECORDER.add_latency("middleware_ms", int((time.perf_counter() - middleware_started) * 1000))

            # --- Check finish reason ---
            if choice.finish_reason == "stop":
                log.info(f"[{self.name}] Finished (stop).")
                trace.finish("stop", iteration)
                exit_reason = "stop"
                break

            if choice.finish_reason == "length":
                log.warning(f"[{self.name}] Output truncated (max_tokens hit).")
                trace.error("length_truncated", "max_tokens hit")
                # If tool calls were present, they were already executed above.
                # Only tell the model they weren't executed if none were parsed
                # (i.e. the truncation cut off the tool call JSON itself).
                if msg.tool_calls:
                    messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] Your response was truncated (token limit), but your tool calls "
                            "WERE executed successfully. The results are above. "
                            "If you had more tool calls planned, continue with the remaining ones now. "
                            "Do NOT re-run the tools that already executed."
                        ),
                        "_metrics_category": "other",
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] Your last response was cut off because it exceeded the token limit. "
                            "No tool calls were executed. "
                            "Please retry, but split large files into smaller parts:\n"
                            "1. Write the first half of the file with write_file\n"
                            "2. Then write the second half as a separate file or append\n"
                            "Or simplify the implementation to fit in one response."
                        ),
                        "_metrics_category": "other",
                    })

        else:
            log.warning(f"[{self.name}] Hit max iterations ({config.MAX_AGENT_ITERATIONS}).")
            trace.finish("max_iterations", config.MAX_AGENT_ITERATIONS)

        return AgentRunResult(last_text, exit_reason, iterations)

    def _should_finalize_for_time(self, elapsed_seconds: float) -> bool:
        if self.name != "evaluator" or self.time_budget is None:
            return False
        remaining = self.time_budget - elapsed_seconds
        return 0 < remaining <= EVALUATOR_FINALIZATION_RESERVE_SECONDS


def _truncate(s: str, n: int) -> str:
    return s[:n] + "..." if len(s) > n else s


def _filter_tool_schemas(schemas: list[dict], allowed_names: set[str]) -> list[dict]:
    return [
        schema for schema in schemas
        if schema.get("function", {}).get("name") in allowed_names
    ]


def _canonical_emit(run_id: str | None, event_type: str, payload: dict, *, role: str, phase: str | None) -> None:
    if run_id:
        emit_event(tools.current_workspace(), run_id, event_type, payload, role=role, phase=phase)


def _usage_value(usage, *names: str) -> int:
    for name in names:
        value = getattr(usage, name, None) if usage is not None else None
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        if value is not None:
            return int(value or 0)
    return 0


def _usage_nested_value(usage, group: str, name: str) -> int:
    value = getattr(usage, group, None) if usage is not None else None
    if value is None and isinstance(usage, dict):
        value = usage.get(group)
    nested = getattr(value, name, None) if value is not None else None
    if nested is None and isinstance(value, dict):
        nested = value.get(name)
    return int(nested or 0)


def _mark_unattributed_messages(messages: list[dict], category: str) -> None:
    for msg in messages:
        if msg.get("role") == "system":
            msg.setdefault("_metrics_category", "static_system_prompt")
        else:
            msg.setdefault("_metrics_category", category)


def _messages_for_api(messages: list[dict]) -> list[dict]:
    return [
        {key: value for key, value in msg.items() if not key.startswith("_metrics_")}
        for msg in messages
    ]


def _categorize_messages_for_metrics(messages: list[dict]) -> dict[str, int]:
    categories = {
        "static_system_prompt": 0,
        "dynamic_task_state": 0,
        "assistant_history": 0,
        "tool_results": 0,
        "tool_call_arguments": 0,
        "middleware_injections": 0,
        "compression_reset": 0,
        "other": 0,
    }
    for msg in messages:
        category = msg.get("_metrics_category") or _infer_metrics_category(msg)
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        categories[category] = categories.get(category, 0) + metrics.estimate_text_tokens(str(content))
        for tc in msg.get("tool_calls", []):
            args = str(tc.get("function", {}).get("arguments", ""))
            categories["tool_call_arguments"] += metrics.estimate_text_tokens(args)
    return categories


def _infer_metrics_category(msg: dict) -> str:
    role = msg.get("role")
    content = str(msg.get("content") or "")
    if role == "system":
        return "static_system_prompt"
    if role == "tool":
        return "tool_results"
    if role == "assistant":
        return "assistant_history"
    if "[COMPACTED CONTEXT" in content or "handoff document" in content:
        return "compression_reset"
    if content.startswith("[SYSTEM]") or content.startswith("[MANDATORY]"):
        return "middleware_injections"
    if role == "user":
        return "dynamic_task_state"
    return "other"
