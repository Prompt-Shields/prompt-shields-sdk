from unittest.mock import MagicMock

from prompt_shields.providers import (
    AnthropicAdapter,
    OpenAIAdapter,
    get_adapter,
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
