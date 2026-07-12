import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class MetricsTests(unittest.TestCase):
    def test_recorder_writes_run_phase_llm_tool_and_cache_metrics(self):
        import metrics

        original = metrics.RECORDER
        recorder = metrics.MetricsRecorder()
        try:
            metrics.RECORDER = recorder
            with tempfile.TemporaryDirectory() as tmp:
                recorder.start_run("run-1", tmp, "terminal")
                recorder.start_phase("build")
                recorder.record_agent_round("builder", "build", 2)
                recorder.record_context_event("compact")
                recorder.record_token_attribution(
                    role="builder",
                    phase="build",
                    estimated_input_tokens=90,
                    categories={
                        "static_system_prompt": 10,
                        "dynamic_task_state": 20,
                        "assistant_history": 5,
                        "tool_results": 15,
                        "tool_call_arguments": 7,
                        "middleware_injections": 3,
                        "compression_reset": 0,
                        "other": 0,
                    },
                )
                recorder.add_latency("context_processing_ms", 2)
                recorder.add_latency("middleware_ms", 3)
                recorder.record_llm_call(
                    role="builder",
                    phase="build",
                    model="test-model",
                    latency_ms=123,
                    usage=SimpleNamespace(
                        prompt_tokens=100,
                        completion_tokens=25,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=40),
                    ),
                )
                recorder.record_tool_call(
                    role="builder",
                    phase="build",
                    tool_name="read_file",
                    arguments={"path": "a.txt"},
                    latency_ms=5,
                    result="hello world",
                )
                recorder.record_tool_call(
                    role="builder",
                    phase="build",
                    tool_name="read_file",
                    arguments={"path": "a.txt"},
                    latency_ms=4,
                    result="[error] failed",
                )
                recorder.record_failure_evidence(
                    {
                        "failure_type": "test_failure",
                        "failure_signature": "test_failure:abc",
                        "same_failure_count": 1,
                        "recovery_strategy": "targeted_fix",
                    },
                    recovery_attempt_planned=True,
                )
                recorder.record_recovery_attempt()
                recorder.record_recovery_result(task_success=True)
                recorder.end_phase("build")
                recorder.finish_run(task_success=True, verification_success=True)

                payload = json.loads((Path(tmp) / ".harness" / "metrics.json").read_text(encoding="utf-8"))
        finally:
            metrics.RECORDER = original

        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["summary"]["llm_call_count"], 1)
        self.assertEqual(payload["summary"]["tool_call_count"], 2)
        self.assertEqual(payload["summary"]["total_input_tokens"], 100)
        self.assertEqual(payload["summary"]["total_output_tokens"], 25)
        self.assertEqual(payload["summary"]["total_cached_tokens"], 40)
        self.assertEqual(payload["summary"]["cache_hit_ratio"], 0.4)
        attribution = payload["summary"]["token_attribution"]
        self.assertEqual(attribution["static_system_prompt"], 10)
        self.assertEqual(attribution["dynamic_task_state"], 20)
        self.assertEqual(attribution["tool_results"], 15)
        self.assertEqual(attribution["tool_call_arguments"], 7)
        self.assertEqual(attribution["middleware_injections"], 3)
        self.assertEqual(attribution["other"], 40)
        latency = payload["summary"]["latency_attribution"]
        self.assertEqual(latency["llm_calls_ms"], 123)
        self.assertEqual(latency["tool_execution_ms"], 9)
        self.assertEqual(latency["context_processing_ms"], 2)
        self.assertEqual(latency["middleware_ms"], 3)
        self.assertEqual(payload["summary"]["agent_rounds"], 2)
        self.assertEqual(payload["context_compression_count"], 1)
        self.assertEqual(payload["repeated_tool_call_count"], 1)
        self.assertEqual(payload["summary"]["failed_attempt_count"], 1)
        self.assertEqual(payload["summary"]["recovery_attempt_count"], 1)
        self.assertEqual(payload["summary"]["recovery_success_count"], 1)
        self.assertEqual(payload["summary"]["recovery_success_rate"], 1.0)
        self.assertEqual(payload["summary"]["retries_per_successful_recovery"], 1.0)
        self.assertTrue(payload["task_success"])
        self.assertTrue(payload["verification_success"])
        self.assertIn("phase_wall_time_ms", payload["phases"]["build"])


if __name__ == "__main__":
    unittest.main()
