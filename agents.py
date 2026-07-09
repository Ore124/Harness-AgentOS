"""
Agent implementation — the core while loop with tool use.
Uses OpenAI-compatible chat completions API with function calling.
"""
from __future__ import annotations

import json
import time
import logging
from pathlib import Path
from openai import OpenAI

import config
import tools
import context

log = logging.getLogger("harness")


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
        trace_dir = Path(config.WORKSPACE)
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

    def iteration(self, n: int, tokens: int):
        self._write("iteration", {"n": n, "tokens": tokens})

    def llm_response(self, content: str | None, tool_calls: list | None, finish_reason: str | None):
        self._write("llm_response", {
            "content": (content or "")[:500],
            "tool_calls": [tc["function"]["name"] for tc in (tool_calls or [])],
            "finish_reason": finish_reason,
        })

    def tool_call(self, name: str, args: dict, result: str):
        self._write("tool_call", {
            "tool": name,
            "args": _truncate(json.dumps(args, ensure_ascii=False), 300),
            "result": _truncate(result, 500),
        })

    def middleware_inject(self, source: str, hook: str, message: str):
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
            resp = get_client().chat.completions.create(
                model=config.MODEL,
                messages=messages,
                max_tokens=10000,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            err_str = str(e)
            if ("rate_limit" in err_str.lower() or "429" in err_str) and attempt < 3:
                wait = min(2 ** (attempt + 1), 30) + random.uniform(0, 3)
                log.warning(f"llm_call_simple rate limited, waiting {wait:.1f}s (attempt {attempt+1}/4)")
                time.sleep(wait)
                continue
            log.error(f"llm_call_simple failed: {e}")
            # Return a minimal summary rather than crashing
            return "[context summarization failed — continuing with truncated context]"
    return "[context summarization failed after retries]"


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------

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

    def run(self, task: str) -> str:
        """
        Execute the agent loop until the model stops calling tools
        or we hit the iteration limit.

        Returns the final assistant text response.
        Writes a JSONL trace file to {WORKSPACE}/_trace_{name}.jsonl
        """
        trace = TraceWriter(self.name)

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]

        client = get_client()
        consecutive_errors = 0
        last_text = ""

        for iteration in range(1, config.MAX_AGENT_ITERATIONS + 1):
            # --- Middleware: per-iteration hooks ---
            for mw in self.middlewares:
                inject = mw.per_iteration(iteration, messages)
                if inject:
                    messages.append({"role": "user", "content": inject})
                    trace.middleware_inject(type(mw).__name__, "per_iteration", inject)

            # --- Context lifecycle check ---
            token_count = context.count_tokens(messages)
            log.info(f"[{self.name}] iteration={iteration}  tokens≈{token_count}")
            trace.iteration(iteration, token_count)

            if token_count > config.RESET_THRESHOLD or context.detect_anxiety(messages):
                reason = "anxiety detected" if token_count <= config.RESET_THRESHOLD else f"tokens {token_count} > threshold"
                log.warning(f"[{self.name}] Context reset triggered ({reason}). Writing checkpoint...")
                trace.context_event("reset", reason)
                checkpoint = context.create_checkpoint(messages, llm_call_simple)
                messages = context.restore_from_checkpoint(checkpoint, self.system_prompt)
            elif token_count > config.COMPRESS_THRESHOLD:
                log.info(f"[{self.name}] Compacting context (role={self.name})...")
                trace.context_event("compact", f"tokens={token_count}")
                messages = context.compact_messages(messages, llm_call_simple, role=self.name)

            # --- LLM call ---
            kwargs = dict(
                model=config.MODEL,
                messages=messages,
                max_tokens=8192,
            )
            if self.use_tools:
                base_schemas = self.tool_schemas if self.tool_schemas is not None else tools.TOOL_SCHEMAS
                kwargs["tools"] = base_schemas + self.extra_tool_schemas
                kwargs["tool_choice"] = "auto"
                # Parallel tool calls: only enable for models known to handle it well.
                # Weaker models produce malformed parallel calls that waste time.
                # Controlled via config; default OFF for safety.
                if config.ENABLE_PARALLEL_TOOL_CALLS:
                    kwargs["parallel_tool_calls"] = True

            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as e:
                err_str = str(e)
                trace.error("api_error", err_str)

                # Rate limits get longer backoff and don't count toward abort threshold
                if "rate_limit" in err_str.lower() or "429" in err_str:
                    import random
                    wait = min(2 ** (consecutive_errors + 2), 120) + random.uniform(0, 5)
                    log.warning(f"[{self.name}] Rate limited, waiting {wait:.1f}s...")
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
                    })
                    time.sleep(1)
                    continue

                log.error(f"[{self.name}] API error: {e}")
                consecutive_errors += 1
                if consecutive_errors >= config.MAX_TOOL_ERRORS:
                    log.error(f"[{self.name}] Too many API errors, aborting.")
                    trace.finish("api_errors", iteration)
                    break
                time.sleep(2 ** consecutive_errors)
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
                    break
                time.sleep(2)
                continue

            choice = response.choices[0]
            msg = choice.message

            # --- Append assistant message to history ---
            assistant_msg = {"role": "assistant", "content": msg.content}
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
                        })
                        continue

                forced_continue = False
                for mw in self.middlewares:
                    inject = mw.pre_exit(messages)
                    if inject:
                        messages.append({"role": "user", "content": inject})
                        trace.middleware_inject(type(mw).__name__, "pre_exit", inject)
                        forced_continue = True
                        break
                if forced_continue:
                    continue
                log.info(f"[{self.name}] Finished (no more tool calls).")
                trace.finish("no_tool_calls", iteration)
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
                    })
                    continue

                log.info(f"[{self.name}] tool: {fn_name}({_truncate(str(fn_args), 120)})")
                result = tools.execute_tool(fn_name, fn_args)
                log.debug(f"[{self.name}] tool result: {_truncate(result, 200)}")
                trace.tool_call(fn_name, fn_args, result)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

                # --- Middleware: post-tool hooks ---
                for mw in self.middlewares:
                    inject = mw.post_tool(fn_name, fn_args, result, messages)
                    if inject:
                        # For parallel tool calls, only inject AFTER the last tool
                        # to avoid breaking the tool_call/tool_result sequence
                        if tc == msg.tool_calls[-1]:
                            messages.append({"role": "user", "content": inject})
                            trace.middleware_inject(type(mw).__name__, "post_tool", inject)
                        break

            # --- Check finish reason ---
            if choice.finish_reason == "stop":
                log.info(f"[{self.name}] Finished (stop).")
                trace.finish("stop", iteration)
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
                    })

        else:
            log.warning(f"[{self.name}] Hit max iterations ({config.MAX_AGENT_ITERATIONS}).")
            trace.finish("max_iterations", config.MAX_AGENT_ITERATIONS)

        return last_text


def _truncate(s: str, n: int) -> str:
    return s[:n] + "..." if len(s) > n else s
