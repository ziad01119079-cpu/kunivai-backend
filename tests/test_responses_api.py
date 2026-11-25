import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from openai import APIError

from app.responses_api import Citation, stream_response


class _FakeStream:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


def test_file_search_results_are_returned_as_internal_evidence():
    captured = {}
    response = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=10, output_tokens=4, total_tokens=14),
        output=[
            SimpleNamespace(
                type="file_search_call",
                results=[SimpleNamespace(text="تجدد الهوية عند تحقق شرط موثق")],
            ),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        type="output_text",
                        annotations=[
                            SimpleNamespace(
                                type="file_citation", file_id="file-1", index=0
                            )
                        ],
                    )
                ],
            ),
        ],
    )
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="إجابة"),
        SimpleNamespace(type="response.completed", response=response),
    ]

    class Responses:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream(events)

    client = SimpleNamespace(responses=Responses())

    async def run():
        return [
            item
            async for item in stream_response(
                [{"role": "user", "content": "سؤال"}],
                "prompt",
                tools=[{"type": "file_search", "vector_store_ids": ["vs-test"]}],
            )
        ]

    with (
        patch("app.responses_api.get_openai_client", return_value=client),
        patch(
            "app.responses_api.get_config",
            return_value=SimpleNamespace(OPENAI_MODEL="test-model"),
        ),
        patch(
            "app.responses_api._resolve_citations",
            new=AsyncMock(return_value=[Citation("file-1", "policy.pdf", 0)]),
        ),
    ):
        output = asyncio.run(run())

    assert captured["include"] == ["file_search_call.results"]
    assert captured["tool_choice"] == "required"
    done = json.loads(output[-1][6:].strip())
    assert done["evidence"] == ["تجدد الهوية عند تحقق شرط موثق"]
    assert done["citations"][0]["filename"] == "policy.pdf"


def test_non_retrieval_request_omits_tool_choice_and_include():
    captured = {}
    response = SimpleNamespace(usage=None, output=[])
    events = [SimpleNamespace(type="response.completed", response=response)]

    class Responses:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream(events)

    client = SimpleNamespace(responses=Responses())

    async def run():
        return [
            item
            async for item in stream_response(
                [{"role": "user", "content": "مش فاهم"}],
                "prompt",
                tools=[],
            )
        ]

    with (
        patch("app.responses_api.get_openai_client", return_value=client),
        patch(
            "app.responses_api.get_config",
            return_value=SimpleNamespace(OPENAI_MODEL="test-model"),
        ),
    ):
        output = asyncio.run(run())

    assert captured["tools"] == []
    assert "tool_choice" not in captured
    assert "include" not in captured
    assert json.loads(output[-1][6:].strip()) == {"type": "done", "citations": []}


def test_output_token_limit_is_forwarded_to_provider():
    captured = {}
    response = SimpleNamespace(usage=None, output=[])

    class Responses:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream([SimpleNamespace(type="response.completed", response=response)])

    async def run():
        return [item async for item in stream_response([], "prompt", tools=[], max_output_tokens=220)]

    with (
        patch("app.responses_api.get_openai_client", return_value=SimpleNamespace(responses=Responses())),
        patch("app.responses_api.get_config", return_value=SimpleNamespace(OPENAI_MODEL="test-model")),
    ):
        asyncio.run(run())
    assert captured["max_output_tokens"] == 220


def test_provider_failure_returns_sanitized_typed_error(caplog):
    class Responses:
        def stream(self, **_kwargs):
            raise RuntimeError("secret upstream detail")

    async def run():
        return [item async for item in stream_response([], "prompt", tools=[])]

    with (
        patch("app.responses_api.get_openai_client", return_value=SimpleNamespace(responses=Responses())),
        patch("app.responses_api.get_config", return_value=SimpleNamespace(OPENAI_MODEL="test-model")),
    ):
        output = asyncio.run(run())
    payload = json.loads(output[-1][6:].strip())
    assert payload["code"] == "LLM_UNAVAILABLE"
    assert payload["retryable"] is True
    assert "secret" not in payload["message"]
    assert "secret upstream detail" not in caplog.text


def _provider_stream_error(code):
    return APIError(
        "private upstream detail",
        request=httpx.Request("POST", "https://example.invalid/responses"),
        body={"error": {"code": code, "message": "private upstream detail"}},
    )


@pytest.mark.parametrize(
    ("first_events", "next_events", "attempts", "types", "code"),
    [
        ([_provider_stream_error("server_error")], ["done"], 2, ["done"], None),
        ([_provider_stream_error("server_error")], [_provider_stream_error("server_error")],
         2, ["error"], "LLM_UNAVAILABLE"),
        (["chunk", _provider_stream_error("server_error")], [], 1, ["chunk", "error"], "LLM_UNAVAILABLE"),
        ([_provider_stream_error("insufficient_quota")], [], 1, ["error"], "LLM_UNAVAILABLE"),
        ([_provider_stream_error("rate_limit_exceeded")], [], 1, ["error"], "LLM_RATE_LIMITED"),
        ([_provider_stream_error("unrecognized_body")], [], 1, ["error"], "LLM_UNAVAILABLE"),
        ([SimpleNamespace(type="response.failed", response=SimpleNamespace(
            error=SimpleNamespace(code="internal_error")
        ))], ["done"], 2, ["done"], None),
    ],
)
def test_in_band_provider_failure_retry_is_bounded(first_events, next_events, attempts, types, code, caplog):
    calls = []
    done = SimpleNamespace(type="response.completed", response=SimpleNamespace(usage=None, output=[]))
    chunk = SimpleNamespace(type="response.output_text.delta", delta="partial")
    fixture = {"done": done, "chunk": chunk}

    def stream(**kwargs):
        items = first_events if not calls else next_events
        calls.append(kwargs)
        result = _FakeStream([fixture[item] if isinstance(item, str) else item for item in items])
        result._response = SimpleNamespace(headers={"x-request-id": "req_fake123"})
        return result

    async def run():
        return [json.loads(item[6:]) async for item in stream_response([], "prompt", tools=[])]

    with (
        patch("app.responses_api.get_openai_client", return_value=SimpleNamespace(
            responses=SimpleNamespace(stream=stream)
        )),
        patch("app.responses_api.get_config", return_value=SimpleNamespace(OPENAI_MODEL="test-model")),
        patch("app.responses_api.distributed_enabled", return_value=True),
        patch("app.responses_api.asyncio.sleep", new_callable=AsyncMock),
    ):
        output = asyncio.run(run())

    assert len(calls) == attempts
    assert [item["type"] for item in output] == types
    assert output[-1].get("code") == code
    assert "provider_request_id=req_fake123" in caplog.text
    assert "private upstream detail" not in caplog.text


def test_incomplete_output_is_reported_instead_of_false_done():
    response = SimpleNamespace(
        incomplete_details=SimpleNamespace(reason="max_output_tokens")
    )

    class Responses:
        def stream(self, **_kwargs):
            return _FakeStream([
                SimpleNamespace(type="response.output_text.delta", delta="partial"),
                SimpleNamespace(type="response.incomplete", response=response),
            ])

    async def run():
        return [item async for item in stream_response([], "prompt", tools=[])]

    with (
        patch("app.responses_api.get_openai_client", return_value=SimpleNamespace(responses=Responses())),
        patch("app.responses_api.get_config", return_value=SimpleNamespace(OPENAI_MODEL="test-model")),
    ):
        output = asyncio.run(run())
    payload = json.loads(output[-1][6:].strip())
    assert payload["code"] == "OUTPUT_LIMIT_REACHED"
    assert payload["reason"] == "max_output_tokens"
    assert all('"type": "done"' not in item for item in output)


def test_provider_eof_without_completion_is_interruption():
    class Responses:
        def stream(self, **_kwargs):
            return _FakeStream([])

    async def run():
        return [item async for item in stream_response([], "prompt", tools=[])]

    with (
        patch("app.responses_api.get_openai_client", return_value=SimpleNamespace(responses=Responses())),
        patch("app.responses_api.get_config", return_value=SimpleNamespace(OPENAI_MODEL="test-model")),
    ):
        output = asyncio.run(run())
    assert json.loads(output[-1][6:].strip())["code"] == "LLM_STREAM_INTERRUPTED"



def test_existing_inline_source_is_not_repeated_and_done_keeps_it():
    url = "https://do.ku.edu.kw/ar/academic-calendar-0"
    inline = f"راجع [التقويم الرسمي]({url})."
    ann = SimpleNamespace(type="url_citation", url=url, title="التقويم الرسمي", start_index=0, end_index=len(inline))
    response = SimpleNamespace(usage=None, output=[
        SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", annotations=[ann])]),
    ])
    events = [
        SimpleNamespace(type="response.output_text.delta", delta=inline),
        SimpleNamespace(type="response.completed", response=response),
    ]
    client = SimpleNamespace(responses=SimpleNamespace(stream=lambda **_kwargs: _FakeStream(events)))

    async def run():
        return [json.loads(item[6:]) async for item in stream_response(
            [{"role": "user", "content": "متى فترة التحويل؟"}], "prompt", tools=[]
        )]

    with (
        patch("app.responses_api.get_openai_client", return_value=client),
        patch("app.responses_api.get_config", return_value=SimpleNamespace(OPENAI_MODEL="gpt-4o")),
    ):
        payloads = asyncio.run(run())

    assert [item["type"] for item in payloads] == ["chunk", "done"]
    assert payloads[0]["content"] == inline
    assert payloads[-1]["citations"][0]["url"] == url


def test_retrieval_ceiling_and_followup_depth_are_not_clipped_by_short_default():
    calls = []
    response = SimpleNamespace(usage=None, output=[])
    events = [SimpleNamespace(type="response.completed", response=response)]

    def capture(**kwargs):
        calls.append(kwargs)
        return _FakeStream(events)

    async def run():
        async for _ in stream_response([{"role": "user", "content": "متى الموعد؟"}], "prompt", tools=[{"type": "web_search"}]):
            pass
        async for _ in stream_response([
            {"role": "assistant", "content": "المشتقة معدل التغير"},
            {"role": "user", "content": "اشرح أكتر"},
        ], "prompt", tools=[]):
            pass

    with (
        patch("app.responses_api.get_openai_client", return_value=SimpleNamespace(responses=SimpleNamespace(stream=capture))),
        patch("app.responses_api.get_config", return_value=SimpleNamespace(OPENAI_MODEL="gpt-4o")),
    ):
        asyncio.run(run())

    assert calls[0]["max_output_tokens"] >= 850
    assert calls[1]["max_output_tokens"] > calls[0]["max_output_tokens"]
