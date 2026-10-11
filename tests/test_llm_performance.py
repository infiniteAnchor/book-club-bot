"""Offline concurrency, alert delivery, and content-free telemetry regressions."""
import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

import llm
import telemetry


def test_slow_llm_does_not_block_event_loop():
    entered = threading.Event()
    release = threading.Event()

    def provider():
        entered.set()
        assert release.wait(2), "event loop did not release the provider"
        return "done"

    async def scenario():
        task = asyncio.create_task(llm.run(provider))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            assert entered.is_set()
            assert not task.done()
            release.set()
            assert await task == "done"
        finally:
            release.set()
            await task

    asyncio.run(scenario())


def test_worker_provider_failure_delivers_alert_on_event_loop():
    class Unauthorized(Exception):
        status_code = 401

    async def scenario():
        loop_thread = threading.get_ident()
        delivered = asyncio.Event()

        def handler(failure):
            assert threading.get_ident() == loop_thread
            assert failure["category"] == "authentication"
            delivered.set()

        with mock.patch.object(llm, "_error_handler", handler), \
             mock.patch.object(llm, "_get_client", side_effect=Unauthorized("private provider text")):
            with pytest.raises(Unauthorized):
                await llm.run(llm._call_llm, "private system", "private prompt", 10)
            await asyncio.wait_for(delivered.wait(), 1)

    asyncio.run(scenario())


def test_concurrent_requests_have_separate_counts_and_safe_logs(caplog):
    barrier = threading.Barrier(2)

    def provider(system, prompt, max_tokens):
        barrier.wait(timeout=2)
        telemetry.record_llm("test", 0.125, prompt == "private failure", 12, 4)
        if prompt == "private failure":
            raise RuntimeError(prompt)
        return "private response"

    async def request(prompt, second_call=False):
        token = telemetry.start_request()
        metrics = telemetry.current_request.get()
        try:
            try:
                await llm.run(provider, "private system", prompt, 10)
            except RuntimeError:
                telemetry.mark_failure()
            if second_call:
                await llm.run(telemetry.record_llm, "test", 0.25, False, 5, 2)
            return metrics
        finally:
            telemetry.finish_request(token, "query")
            assert telemetry.current_request.get() is None

    async def scenario():
        return await asyncio.gather(request("private success", True), request("private failure"))

    with caplog.at_level(logging.INFO, logger="bookclub.performance"):
        first, second = asyncio.run(scenario())
    assert first.request_id != second.request_id
    assert (first.llm_calls, first.llm_failures, first.llm_seconds) == (2, 0, 0.375)
    assert (second.llm_calls, second.llm_failures, second.failed) == (1, 1, True)
    assert "private" not in caplog.text
    assert "llm_calls=2" in caplog.text
    assert "input_tokens=12 output_tokens=4" in caplog.text


@pytest.mark.parametrize("provider", ["anthropic", "openai", "deepseek"])
def test_provider_clients_have_explicit_timeout_and_no_automatic_retries(provider):
    target = "anthropic.Anthropic" if provider == "anthropic" else "openai.OpenAI"
    with mock.patch.object(llm, "LLM_PROVIDER", provider), \
         mock.patch.object(llm, "_clients", {}), mock.patch(target) as constructor:
        llm._get_client()
        llm._get_client()
    constructor.assert_called_once()
    assert constructor.call_args.kwargs["timeout"] == 30.0
    assert constructor.call_args.kwargs["max_retries"] == 0


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_actual_provider_call_records_usage_and_failure(provider, caplog):
    usage = SimpleNamespace(input_tokens=12, output_tokens=4, prompt_tokens=12, completion_tokens=4)
    response = SimpleNamespace(
        usage=usage, content=[SimpleNamespace(text="private response")],
        choices=[SimpleNamespace(message=SimpleNamespace(content="private response"))],
    )
    client = mock.Mock()
    create = client.messages.create if provider == "anthropic" else client.chat.completions.create
    create.side_effect = [response, RuntimeError("private error")]
    token = telemetry.start_request()
    metrics = telemetry.current_request.get()
    try:
        with caplog.at_level(logging.INFO, logger="bookclub.performance"), \
             mock.patch.object(llm, "LLM_PROVIDER", provider), mock.patch.object(llm, "_get_client", return_value=client):
            assert llm._call_llm("private system", "private message", 10) == "private response"
            with pytest.raises(RuntimeError):
                llm._call_llm("private system", "private message", 10)
        assert metrics.llm_calls == 2
        assert metrics.llm_failures == 1
        assert metrics.llm_seconds >= 0
        assert "input_tokens=12 output_tokens=4" in caplog.text
        assert "private" not in caplog.text
    finally:
        telemetry.finish_request(token)


def _anthropic_client(usage):
    client = mock.Mock()
    client.messages.create.return_value = SimpleNamespace(
        usage=usage, content=[SimpleNamespace(text="{}")],
    )
    return client


@pytest.mark.parametrize("tone, suffix", [(None, None), ("roast", llm.TONE_INSTRUCTIONS["roast"])])
def test_intent_call_caches_base_prompt_and_keeps_tone_after_breakpoint(tone, suffix):
    client = _anthropic_client(SimpleNamespace(input_tokens=1, output_tokens=1))
    context = {"tone_override": tone} if tone else {}
    with mock.patch.object(llm, "LLM_PROVIDER", "anthropic"), \
         mock.patch.object(llm, "_get_client", return_value=client):
        llm.get_intent("what am I reading?", context)
    system = client.messages.create.call_args.kwargs["system"]
    assert system[0] == {
        "type": "text", "text": llm.SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"},
    }
    if suffix:
        assert system[1:] == [{"type": "text", "text": suffix}]
    else:
        assert len(system) == 1


def test_uncached_calls_and_openai_keep_a_plain_system_string():
    client = _anthropic_client(SimpleNamespace(input_tokens=1, output_tokens=1))
    with mock.patch.object(llm, "LLM_PROVIDER", "anthropic"), \
         mock.patch.object(llm, "_get_client", return_value=client):
        llm._call_llm("recap system", "prompt", 10)
    assert client.messages.create.call_args.kwargs["system"] == "recap system"

    openai = mock.Mock()
    openai.chat.completions.create.return_value = SimpleNamespace(
        usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    with mock.patch.object(llm, "LLM_PROVIDER", "openai"), \
         mock.patch.object(llm, "_get_client", return_value=openai):
        llm._call_llm("base", "prompt", 10, system_suffix=" tone", cache_system=True)
    messages = openai.chat.completions.create.call_args.kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "base tone"}


def test_cache_token_usage_is_logged(caplog):
    usage = SimpleNamespace(
        input_tokens=40, output_tokens=5, cache_read_input_tokens=2500, cache_creation_input_tokens=0,
    )
    with caplog.at_level(logging.INFO, logger="bookclub.performance"), \
         mock.patch.object(llm, "LLM_PROVIDER", "anthropic"), \
         mock.patch.object(llm, "_get_client", return_value=_anthropic_client(usage)):
        llm._call_llm("system", "prompt", 10, cache_system=True)
    assert "cache_read_tokens=2500 cache_write_tokens=0" in caplog.text
