"""Detect native tool-call markup that leaked into assistant text.

Some models (DeepSeek V4's DSML, Qwen/Hermes ``<tool_call>``) serialize tool
calls as text. The serving engine turns well-formed blocks into structured
``tool_calls``; a malformed block (bad JSON inside, a mangled attribute) is
left in ``content``. Without a check the agent loop treats that text as the
turn's final answer: nothing executes, and cron marks the run "ok".

Observed 2026-09-30 on DeepSeek-V4-Flash via TensorFold (three cron runs,
all logged "ok", no file written).
"""

from __future__ import annotations

import re

# Opening marker of a native tool-call block at a line boundary. Line-anchored
# so prose that mentions the syntax mid-sentence is not flagged.
_LEAK_OPENERS = re.compile(
    r"(?m)^[ \t]*<(?:[|\uff5c]{1,2}DSML[|\uff5c]{1,2}\s*(?:tool_calls|calls|invoke)\b"
    r"|tool_call>|function_calls>)"
)

MAX_RETRIES = 2

RETRY_NUDGE = (
    "Your previous reply contained a tool call written as text that could not "
    "be parsed, so it was NOT executed. Re-issue the tool call with valid JSON "
    "arguments. For large file contents, split the work into several smaller "
    "write_file calls."
)


def has_leaked_tool_markup(text: str | None) -> bool:
    """True when ``text`` carries an unparsed native tool-call block."""
    if not text or not isinstance(text, str):
        return False
    return bool(_LEAK_OPENERS.search(text))


def failure_message(retries: int) -> str:
    return (
        "Model emitted malformed tool-call markup that the server could not parse "
        f"({retries} corrective retries exhausted); no tool was executed."
    )
