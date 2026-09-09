"""Unit tests for ``DeepSeekV4RawProvider`` and its DSML encode/parse loop.

Completions used in parse tests are rendered through the vendored official
encoder itself, so the fixtures cannot drift from the format the parser
expects.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from raven.providers import _dsv4_encoding as enc
from raven.providers.deepseek_v4_raw_provider import (
    DeepSeekV4RawProvider,
    build_dsv4_messages,
    parse_completion,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "exec",
            "description": "Run a shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}},
                "required": ["command"],
            },
        },
    }
]


def _render_assistant(msg: dict) -> str:
    """Render one assistant turn exactly as the model would emit it."""
    return enc.render_message(0, [msg], thinking_mode="thinking")


# ---------------------------------------------------------------------------
# build_dsv4_messages
# ---------------------------------------------------------------------------


def test_tools_attach_to_system_message() -> None:
    msgs = build_dsv4_messages([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], TOOLS)
    assert msgs[0]["tools"] == TOOLS
    assert "tools" not in msgs[1]


def test_tools_synthesize_system_when_absent() -> None:
    msgs = build_dsv4_messages([{"role": "user", "content": "hi"}], TOOLS)
    assert msgs[0]["role"] == "system" and msgs[0]["tools"] == TOOLS


def test_dict_arguments_serialized_to_json_string() -> None:
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "x1", "type": "function", "function": {"name": "exec", "arguments": {"command": "ls"}}}
            ],
        }
    ]
    msgs = build_dsv4_messages(history, None)
    args = msgs[0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, str) and json.loads(args) == {"command": "ls"}


# ---------------------------------------------------------------------------
# encode: full conversation shapes
# ---------------------------------------------------------------------------


def test_encode_tool_conversation_round() -> None:
    messages = build_dsv4_messages(
        [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "need ls",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": json.dumps({"command": "ls", "timeout": 60})},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "file_a\nfile_b"},
        ],
        TOOLS,
    )
    prompt = enc.encode_messages(messages, thinking_mode="thinking", reasoning_effort="max")
    assert prompt.startswith(enc.bos_token + enc.REASONING_EFFORT_PROMPTS["max"])
    assert "### Available Tool Schemas" in prompt
    assert f'<{enc.dsml_token}invoke name="exec">' in prompt
    assert "<tool_result>file_a\nfile_b</tool_result>" in prompt
    # Generation resumes in thinking mode after the tool result.
    assert prompt.endswith(enc.ASSISTANT_SP_TOKEN + enc.thinking_start_token)


# ---------------------------------------------------------------------------
# parse_completion
# ---------------------------------------------------------------------------


def test_parse_plain_answer() -> None:
    text = _render_assistant({"role": "assistant", "content": "done.", "reasoning_content": "thought"})
    parsed = parse_completion(text)
    assert parsed["reasoning_content"] == "thought"
    assert parsed["content"] == "done."
    assert parsed["tool_calls"] == []


def test_parse_tool_call_completion() -> None:
    text = _render_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "run it",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": json.dumps({"command": "ls -la", "timeout": 60})},
                }
            ],
        }
    )
    parsed = parse_completion(text)
    assert len(parsed["tool_calls"]) == 1
    call = parsed["tool_calls"][0]["function"]
    assert call["name"] == "exec"
    assert json.loads(call["arguments"]) == {"command": "ls -la", "timeout": 60}


def test_parse_pre_split_gateway_text() -> None:
    """OpenRouter /completions splits reasoning out; text starts at content."""
    full = _render_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "r",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": json.dumps({"command": "pwd"})},
                }
            ],
        }
    )
    pre_split = full.split(enc.thinking_end_token, 1)[1]
    parsed = parse_completion(pre_split, reasoning_pre_split=True)
    assert parsed["tool_calls"][0]["function"]["name"] == "exec"
    assert parsed["reasoning_content"] == ""


def test_parse_missing_eos_is_tolerated() -> None:
    parsed = parse_completion("some thoughts</think>the answer")
    assert parsed["reasoning_content"] == "some thoughts"
    assert parsed["content"] == "the answer"


def test_parse_truncated_tool_block_degrades_to_content() -> None:
    text = f"thinking</think>partial<{enc.dsml_token}{enc.tool_calls_block_name}>\n<{enc.dsml_token}invoke"
    parsed = parse_completion(text)
    assert parsed["tool_calls"] == []
    assert parsed["reasoning_content"] == "thinking"
    assert parsed["content"].startswith("partial")


# ---------------------------------------------------------------------------
# chat() end to end against a local /completions echo
# ---------------------------------------------------------------------------


class _Server:
    def __init__(self, completion_text: str) -> None:
        captured: dict = {}
        self.captured = captured

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length", 0))
                captured["path"] = self.path
                captured["body"] = json.loads(self.rfile.read(n))
                captured["auth"] = self.headers.get("Authorization")
                resp = json.dumps(
                    {
                        "choices": [{"text": completion_text, "finish_reason": "stop", "index": 0}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()


def test_chat_end_to_end_tool_call() -> None:
    completion = _render_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "let me look",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": json.dumps({"command": "pwd"})},
                }
            ],
        }
    )
    server = _Server(completion)
    try:
        provider = DeepSeekV4RawProvider(
            api_key="k",
            api_base=server.base,
            extra_body={"top_p": 0.95, "reasoning": {"effort": "max"}},
        )
        response = asyncio.run(
            provider.chat(
                [{"role": "user", "content": "where am I"}],
                tools=TOOLS,
                model="deepseek/deepseek-v4-flash-0731",
                max_tokens=100,
                temperature=1.0,
                reasoning_effort="max",
            )
        )
    finally:
        server.close()

    body = server.captured["body"]
    assert server.captured["path"] == "/v1/completions"
    assert server.captured["auth"] == "Bearer k"
    assert body["prompt"].startswith(enc.bos_token + enc.REASONING_EFFORT_PROMPTS["max"])
    assert body["prompt"].endswith(enc.ASSISTANT_SP_TOKEN + enc.thinking_start_token)
    assert body["top_p"] == 0.95
    assert "reasoning" not in body  # effort lives in the prompt prefix, not the body

    assert response.finish_reason == "stop"
    assert response.reasoning_content == "let me look"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "exec"
    assert response.tool_calls[0].arguments == {"command": "pwd"}
    assert response.usage["completion_tokens"] == 5
