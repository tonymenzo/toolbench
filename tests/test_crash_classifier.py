import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from toolbench.core.crash_classifier import classify_crash  # noqa: E402
from toolbench.core.failure_modes import (  # noqa: E402
    AGENT_CRASH, CONTEXT_LENGTH_EXCEEDED, MODEL_FORMAT_CRASH, RATE_LIMITED,
)


# Pasted from a real gpt-oss-120b run for fidelity.
_GPT_OSS_TRACEBACK = """\
Traceback (most recent call last):
  File "/.../eval/core/runner.py", line 140, in run_trial
    response = agent.run(prompt, max_iterations=self.max_iterations)
  File "/.../orchestral/agent/agent.py", line 108, in run
    llm_response = self.llm.get_response(self.context, **llm_kwargs)
  File "/.../orchestral/llm/base/llm.py", line 18, in get_response
    response = self.process_api_response(api_response)
  File "/.../orchestral/llm/openai/client.py", line 89, in process_api_response
    return parse_openai_response(api_response, model_name=self.model)
  File "/.../orchestral/llm/openai/parsers.py", line 59, in parse_openai_response
    tool_calls = parse_tool_calls(tool_calls)
  File "/.../orchestral/llm/openai/parsers.py", line 120, in parse_tool_calls
    arguments=json.loads(call.function.arguments)
  File "/.../python3.13/json/__init__.py", line 352, in loads
    return _default_decoder.decode(s)
"""


def _make_jdce(raw: str, msg: str = "Expecting ',' delimiter"
               ) -> json.JSONDecodeError:
    """Construct a JSONDecodeError by actually attempting to parse `raw`."""
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        # Override the msg so tests can pin a specific text without
        # depending on the exact phrasing CPython picks per release.
        return json.JSONDecodeError(msg, e.doc, e.pos)
    raise AssertionError(f"`{raw!r}` parsed cleanly; couldn't fabricate JDCE")


class TestToolCallJsonDecodeCrash(unittest.TestCase):
    def test_truncated_arguments_classified(self):
        raw = '{"x": 1, "y": 2 "z": 3'  # missing comma
        exc = _make_jdce(raw)
        mode, reason = classify_crash(exc, _GPT_OSS_TRACEBACK)
        self.assertEqual(mode, MODEL_FORMAT_CRASH)
        self.assertIn("malformed tool-call JSON", reason)
        self.assertIn(f"char {exc.pos}", reason)
        self.assertIn(raw[:30], reason)  # snippet shows up

    def test_empty_arguments_classified(self):
        # gpt-oss occasionally emits arguments="" — a real failure mode.
        exc = _make_jdce("",
                         msg="Expecting value")
        mode, reason = classify_crash(exc, _GPT_OSS_TRACEBACK)
        self.assertEqual(mode, MODEL_FORMAT_CRASH)
        self.assertIn("Expecting value", reason)
        self.assertIn("raw=<empty>", reason)

    def test_long_raw_is_truncated(self):
        raw = '{"x":' + "y" * 500
        exc = _make_jdce(raw, msg="Expecting value")
        _, reason = classify_crash(exc, _GPT_OSS_TRACEBACK)
        # Snippet truncated and ellipsized.
        self.assertIn("…", reason)
        self.assertLess(len(reason), 250)

    def test_jsondecode_outside_orchestral_is_not_format_crash(self):
        # Same exception type, but the traceback doesn't go through
        # Orchestral's parser — could be the agent's own python tool
        # decoding bad JSON. Should fall back to AGENT_CRASH.
        traceback_str = (
            'File "user_code.py", line 5, in <module>\n'
            '    json.loads(open("data.json").read())\n'
        )
        exc = _make_jdce("{bad")
        mode, reason = classify_crash(exc, traceback_str)
        self.assertEqual(mode, AGENT_CRASH)
        self.assertIn("JSONDecodeError", reason)


class TestContextLengthCrash(unittest.TestCase):
    # Pasted from the real n004 gpt-oss-120b crash.
    _MSG = ("Error code: 400 - {'error': {'message': 'litellm.BadRequestError: "
            "Hosted_vllmException - {\"error\":{\"message\":\"Input length (133323) "
            "exceeds model's maximum context length (131072).\"")

    def test_classified_with_token_counts(self):
        exc = RuntimeError(self._MSG)  # stand-in for openai.BadRequestError
        mode, reason = classify_crash(exc, "")
        self.assertEqual(mode, CONTEXT_LENGTH_EXCEEDED)
        self.assertIn("133323", reason)
        self.assertIn("131072", reason)

    def test_classified_from_openai_error_code(self):
        exc = RuntimeError("Error: context_length_exceeded")
        mode, reason = classify_crash(exc, "")
        self.assertEqual(mode, CONTEXT_LENGTH_EXCEEDED)
        self.assertIn("context window exceeded", reason)

    def test_unrelated_400_is_not_context_crash(self):
        exc = RuntimeError("Error code: 400 - invalid model parameter 'foo'")
        mode, _ = classify_crash(exc, "")
        self.assertEqual(mode, AGENT_CRASH)


class TestRateLimitCrash(unittest.TestCase):
    def test_sdk_exception_type_name(self):
        # Both the OpenAI and Anthropic SDKs raise `RateLimitError` —
        # match on the type name, no SDK import needed.
        class RateLimitError(Exception):
            pass
        mode, reason = classify_crash(RateLimitError("slow down"), "")
        self.assertEqual(mode, RATE_LIMITED)
        self.assertIn("throttled", reason)

    def test_anthropic_429_message(self):
        exc = RuntimeError(
            "Error code: 429 - {'type': 'error', 'error': "
            "{'type': 'rate_limit_error', 'message': 'Number of request "
            "tokens has exceeded your per-minute rate limit'}}")
        mode, _ = classify_crash(exc, "")
        self.assertEqual(mode, RATE_LIMITED)

    def test_anthropic_overloaded_529(self):
        exc = RuntimeError(
            "Error code: 529 - {'type': 'error', 'error': "
            "{'type': 'overloaded_error', 'message': 'Overloaded'}}")
        mode, _ = classify_crash(exc, "")
        self.assertEqual(mode, RATE_LIMITED)

    def test_openai_quota_message(self):
        exc = RuntimeError(
            "You exceeded your current quota: insufficient_quota")
        mode, _ = classify_crash(exc, "")
        self.assertEqual(mode, RATE_LIMITED)

    def test_unrelated_number_does_not_match(self):
        # '429' embedded in a longer token must not classify.
        exc = RuntimeError("array index 14290 out of bounds for seed4290")
        mode, _ = classify_crash(exc, "")
        self.assertEqual(mode, AGENT_CRASH)

    def test_format_crash_wins_over_rate_limit_text(self):
        # A JSON decode error from the tool-call parser stays a format
        # crash even if the bad payload happens to mention rate limits.
        exc = _make_jdce('{"msg": "rate limit', msg="Unterminated string")
        mode, _ = classify_crash(exc, _GPT_OSS_TRACEBACK)
        self.assertEqual(mode, MODEL_FORMAT_CRASH)


class TestProviderRejectedToolCall(unittest.TestCase):
    """Groq validates tool-call arguments server-side.

    The same gpt-oss defect that raises JSONDecodeError on the OpenAI
    route comes back as HTTP 400 `tool_use_failed` here, with no
    JSONDecodeError anywhere — it must still be MODEL_FORMAT_CRASH so
    the runner spends its format retries on it.
    """

    # Pasted verbatim from a real groq/gpt-oss-120b symbolic trial.
    _BODY = (
        "Error code: 400 - {'error': {'message': 'Failed to parse tool call "
        "arguments as JSON', 'type': 'invalid_request_error', 'code': "
        "'tool_use_failed', 'failed_generation': '{\"name\": \"runpython\", "
        "\"arguments\": import sympy as sp\\n\\n# define symbols\\n'}}"
    )
    _TRACEBACK = (
        'File "/.../orchestral/llm/groq/client.py", line 77, in call_api\n'
        "    api_response = self.client.chat.completions.create(**call_params)\n"
        'File "/.../groq/_base_client.py", line 1071, in request\n'
    )

    def _exc(self):
        # Stand in for groq.BadRequestError without importing the SDK.
        return type("BadRequestError", (Exception,), {})(self._BODY)

    def test_classified_as_format_crash(self):
        mode, _ = classify_crash(self._exc(), self._TRACEBACK)
        self.assertEqual(mode, MODEL_FORMAT_CRASH)

    def test_reason_surfaces_the_offending_generation(self):
        _, reason = classify_crash(self._exc(), self._TRACEBACK)
        self.assertIn("malformed tool-call JSON", reason)
        self.assertIn("runpython", reason)

    def test_unrelated_400_is_not_a_format_crash(self):
        exc = type("BadRequestError", (Exception,), {})(
            "Error code: 400 - {'error': {'message': 'unsupported parameter', "
            "'type': 'invalid_request_error'}}")
        mode, _ = classify_crash(exc, self._TRACEBACK)
        self.assertEqual(mode, AGENT_CRASH)


class TestGenericCrash(unittest.TestCase):
    def test_attribute_error(self):
        exc = AttributeError("'NoneType' object has no attribute 'foo'")
        traceback_str = "File \"x.py\", line 1, in <module>\n"
        mode, reason = classify_crash(exc, traceback_str)
        self.assertEqual(mode, AGENT_CRASH)
        self.assertIn("AttributeError", reason)
        self.assertIn("'foo'", reason)

    def test_long_message_truncated(self):
        exc = RuntimeError("x" * 500)
        mode, reason = classify_crash(exc, "")
        self.assertEqual(mode, AGENT_CRASH)
        self.assertLess(len(reason), 250)
        self.assertTrue(reason.endswith("…"))


if __name__ == "__main__":
    unittest.main()
