import pytest
from prompt_shields.telemetry import MAX_BUFFER_SIZE, TelemetrySender


def test_buffer_enqueue():
    sender = TelemetrySender("http://localhost:8000", "ps-test")
    sender.enqueue({"vendor": "openai"})
    assert len(sender._buffer) == 1


def test_buffer_overflow_drops_oldest():
    sender = TelemetrySender("http://localhost:8000", "ps-test")
    for i in range(MAX_BUFFER_SIZE + 10):
        sender.enqueue({"index": i})
    assert len(sender._buffer) == MAX_BUFFER_SIZE
    assert sender._buffer[0]["index"] == 10


@pytest.mark.asyncio
async def test_flush_empty_buffer():
    sender = TelemetrySender("http://localhost:8000", "ps-test")
    await sender.flush()  # should not raise
    await sender.close()
