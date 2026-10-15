from unittest.mock import MagicMock

from prompt_shields.providers import (
    AnthropicAdapter,
    OpenAIAdapter,
    get_adapter,
    mcp_tool_call,
)


def test_get_adapter_known_vendors():
    assert isinstance(get_adapter("openai"), OpenAIAdapter)
    assert isinstance(get_adapter("anthropic"), AnthropicAdapter)


def test_get_adapter_unknown_raises():
    try:
        get_adapter("nonexistent")
    except ValueError as e:
        assert "nonexistent" in str(e)
    else:
        raise AssertionError("Expected ValueError")


def test_openai_adapter_extracts_tokens():
    response = MagicMock()
    response.usage.prompt_tokens = 100
    response.usage.completion_tokens = 200
    response.choices = []

    fields = OpenAIAdapter().extract(response)
    assert fields["tokens_in"] == 100
    assert fields["tokens_out"] == 200
    assert fields["tool_calls_used"] is None


def test_openai_adapter_extracts_tool_calls():
    response = MagicMock()
    response.usage.prompt_tokens = 50
    response.usage.completion_tokens = 100

    tc = MagicMock()
    tc.function.name = "search_docs"
    msg = MagicMock()
    msg.tool_calls = [tc]
    choice = MagicMock()
    choice.message = msg
    response.choices = [choice]

    fields = OpenAIAdapter().extract(response)
    assert fields["tool_calls_used"] == [{"name": "search_docs", "type": "function"}]


def test_openai_adapter_handles_no_usage():
    response = MagicMock()
    response.usage = None
    response.choices = []
    fields = OpenAIAdapter().extract(response)
    assert fields["tokens_in"] is None
    assert fields["tokens_out"] is None


def test_anthropic_adapter_extracts_tokens():
    response = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 200
    response.content = []

    fields = AnthropicAdapter().extract(response)
    assert fields["tokens_in"] == 100
    assert fields["tokens_out"] == 200


def test_anthropic_adapter_extracts_tool_use_blocks():
    response = MagicMock()
    response.usage.input_tokens = 50
    response.usage.output_tokens = 100

    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "search_db"
    text_block = MagicMock()
    text_block.type = "text"

    response.content = [text_block, tool_block]
    fields = AnthropicAdapter().extract(response)
    assert fields["tool_calls_used"] == [{"name": "search_db", "type": "tool_use"}]


def test_anthropic_adapter_extracts_mcp_tool_use_blocks():
    response = MagicMock()
    response.usage.input_tokens = 50
    response.usage.output_tokens = 100
    mcp_block = MagicMock()
    mcp_block.type = "mcp_tool_use"
    mcp_block.name = "search_issues"
    mcp_block.server_name = "github"
    result_block = MagicMock()
    result_block.type = "mcp_tool_result"
    response.content = [mcp_block, result_block]

    fields = AnthropicAdapter().extract(response)
    assert fields["tool_calls_used"] == [
        {"name": "search_issues", "type": "mcp", "server": "github"}
    ]


def test_bedrock_anthropic_body_mcp_tool_use():
    body = {
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "content": [{"type": "mcp_tool_use", "name": "t", "server_name": "s"}],
    }
    fields = get_adapter("bedrock").extract(body)
    assert fields["tool_calls_used"] == [{"name": "t", "type": "mcp", "server": "s"}]


def test_mcp_tool_call_shapes():
    assert mcp_tool_call({"type": "tool_use", "name": "x"}) is None
    assert mcp_tool_call(None) is None
    assert mcp_tool_call({"type": ["mcp_call"]}) is None  # malformed raw JSON
    assert mcp_tool_call({"type": "mcp_call", "name": 5}) == {
        "name": None, "type": "mcp", "server": None}
    long = mcp_tool_call({"type": "mcp_call", "name": "n" * 500,
                          "server_label": "s" * 500})
    assert len(long["name"]) == 200 and len(long["server"]) == 200
