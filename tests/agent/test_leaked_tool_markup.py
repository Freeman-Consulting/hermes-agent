"""Leaked native tool-call markup must not become a successful final answer.

Regression for 2026-09-30: DeepSeek-V4-Flash (TensorFold) returned malformed DSML
tool-call blocks as plain content three times; each cron run was logged "ok" and
nothing executed. Fixtures below are trimmed from those real outputs.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.leaked_tool_markup import has_leaked_tool_markup
from run_agent import AIAgent

BAR = "\uff5c"

# 09:07 run: well-formed block, argument JSON degenerated mid-string.
LEAK_BAD_JSON = (
    "All 25 bookmarks are new. Let me build the records.\n\n"
    f"<{BAR}DSML{BAR}tool_calls>\n"
    f"<{BAR}DSML{BAR}invoke name=\"write_file\">\n"
    f"<{BAR}DSML{BAR}parameter name=\"arguments\" string=\"false\">"
    "{\"path\": \"/tmp/x.jsonl\", \"content\": \"{\\\"id\\\": \\\"1\\\", \\\"text\\\": \\\"Service names, not clear"
    f"</{BAR}DSML{BAR}parameter>\n</{BAR}DSML{BAR}invoke>\n</{BAR}DSML{BAR}tool_calls>"
)
# 10:46 run: model invented an attribute (database= instead of string=).
LEAK_BAD_ATTR = (
    "Write path is working. Let me check what files exist.\n\n"
    f"<{BAR}DSML{BAR}tool_calls>\n<{BAR}DSML{BAR}invoke name=\"search_files\">\n"
    f"<{BAR}DSML{BAR}parameter name=\"arguments\" database=\"false\">{{\"pattern\": \"*\"}}"
    f"</{BAR}DSML{BAR}parameter>\n</{BAR}DSML{BAR}invoke>\n</{BAR}DSML{BAR}tool_calls>"
)


class TestDetector:
    def test_flags_real_dsml_leaks(self):
        assert has_leaked_tool_markup(LEAK_BAD_JSON)
        assert has_leaked_tool_markup(LEAK_BAD_ATTR)

    def test_flags_ascii_bar_and_generic_tool_call_blocks(self):
        assert has_leaked_tool_markup("<|DSML|tool_calls>\n<|DSML|invoke name=\"x\">")
        assert has_leaked_tool_markup("ok\n<tool_call>\n{\"name\": \"x\"}\n</tool_call>")

    def test_ignores_prose_mentions_and_normal_answers(self):
        assert not has_leaked_tool_markup("Done. File written.")
        assert not has_leaked_tool_markup("DeepSeek emits <｜DSML｜tool_calls> blocks inline.")
        assert not has_leaked_tool_markup("")
        assert not has_leaked_tool_markup(None)


def _resp(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           model="test/model", usage=None)


def _agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                        max_iterations=10, quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._fallback_chain = []
    return agent


def _run(agent, responses):
    agent.client.chat.completions.create.side_effect = responses
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("normalize the bookmarks")


def test_persistent_leak_fails_the_turn_instead_of_answering():
    agent = _agent()
    result = _run(agent, [_resp(LEAK_BAD_JSON), _resp(LEAK_BAD_ATTR), _resp(LEAK_BAD_JSON)])
    assert result["completed"] is False
    assert result["turn_exit_reason"] == "leaked_tool_markup_exhausted"
    assert "DSML" not in result["final_response"]
    assert "no tool was executed" in result["error"]
    assert agent.client.chat.completions.create.call_count == 3


def test_leak_then_recovery_completes_normally():
    agent = _agent()
    result = _run(agent, [_resp(LEAK_BAD_ATTR), _resp("Done: 25 records written.")])
    assert result["completed"] is True
    assert result["final_response"] == "Done: 25 records written."
    # the corrective nudge was sent to the model on the retry
    sent = agent.client.chat.completions.create.call_args_list[1].kwargs["messages"]
    assert any("could not be parsed" in (m.get("content") or "") for m in sent if m["role"] == "user")


def test_clean_answer_is_untouched():
    agent = _agent()
    result = _run(agent, [_resp("All done.")])
    assert result["completed"] is True
    assert result["final_response"] == "All done."
    assert agent.client.chat.completions.create.call_count == 1
