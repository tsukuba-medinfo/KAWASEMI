"""vLLM（OpenAI 互換）アダプタのテスト。

**固定するのは翻訳の契約である。** Ollama の `chat(model, messages, format, options, think)` と
`.message.content` という形へ、OpenAI 互換 API を寄せる。`format` が `response_format` に
写らなければスキーマの必須キーが効かず、`think` が写らなければ固定した
`DEFAULT_THINK` が効いていない記録ができる。

ネットワークへは出ない（`httpx.MockTransport` で差し替える）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kawasemi.ai_input.llm_json import LLMCallRecord, chat_json
from kawasemi.ai_input.llm_provider import (
    PROVIDER_VLLM,
    design_llm_model,
    endpoint_of,
    make_design_llm_client,
    model_digest_of,
    provider_of,
)
from kawasemi.ai_input.vllm_client import VllmClient, VllmContextTooSmall, parse_metrics
from kawasemi.core.config import settings
from kawasemi.core.exceptions import AIServiceException

BASE = "http://vllm.test:18300"
MODEL = "model-d-flash"
ROOT = "/hf/hub/models--example--Model-D-Flash/snapshots/0123abcd"


def _metrics(running: int, waiting: int, stop: int, length: int) -> str:
    labels = f'engine="0",model_name="{MODEL}"'
    return "\n".join(
        [
            "# HELP vllm:num_requests_running Number of requests in model execution batches.",
            f"vllm:num_requests_running{{{labels}}} {running}.0",
            f"vllm:num_requests_waiting{{{labels}}} {waiting}.0",
            f'vllm:num_requests_waiting_by_reason{{{labels},reason="capacity"}} 9.0',
            f'vllm:request_success_total{{engine="0",finished_reason="stop",model_name="{MODEL}"}} {stop}.0',
            f'vllm:request_success_total{{engine="0",finished_reason="length",model_name="{MODEL}"}} {length}.0',
            'vllm:num_requests_running{engine="0",model_name="other"} 7.0',
        ]
    )


class _Server:
    """`/v1/models`・`/metrics`・`/v1/chat/completions` を持つ偽のサーバ。"""

    def __init__(
        self,
        *,
        completion: dict[str, Any],
        max_model_len: int = 500000,
        metrics: list[str] | None = None,
        metrics_status: int = 200,
    ) -> None:
        self.completion = completion
        self.max_model_len = max_model_len
        self.metrics = list(metrics or [_metrics(0, 0, 10, 1), _metrics(0, 0, 11, 1)])
        self.metrics_status = metrics_status
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": MODEL, "root": ROOT, "max_model_len": self.max_model_len}]},
            )
        if path == "/metrics":
            if self.metrics_status != 200:
                return httpx.Response(self.metrics_status)
            return httpx.Response(200, text=self.metrics.pop(0))
        if path == "/v1/chat/completions":
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=self.completion)
        return httpx.Response(404)

    def client(self, max_tokens: int = 32768) -> VllmClient:
        return VllmClient(
            base_url=BASE,
            model=MODEL,
            max_tokens=max_tokens,
            timeout=10,
            transport=httpx.MockTransport(self.handler),
        )


def _completion(
    content: str | None, *, reasoning: str | None = "考えた", finish: str = "stop"
) -> dict:
    return {
        "choices": [
            {"message": {"content": content, "reasoning": reasoning}, "finish_reason": finish}
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 45},
    }


SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


async def test_request_translates_format_think_and_options() -> None:
    """`format` は json_schema に、`think` は enable_thinking に、temperature と seed はそのまま写る。"""
    server = _Server(completion=_completion('{"a": "x"}'))
    await server.client(max_tokens=4096).chat(
        model=MODEL,
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        format=SCHEMA,
        options={"temperature": 0, "seed": 0, "num_ctx": 32768},
        think=True,
    )
    (body,) = server.requests
    assert body["model"] == MODEL
    assert body["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "result", "schema": SCHEMA},
    }
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["temperature"] == 0
    assert body["seed"] == 0
    assert body["max_tokens"] == 4096
    # num_ctx はリクエストでは変えられないので送らない（窓の検査に使う）
    assert "num_ctx" not in body


async def test_think_false_is_sent_and_none_is_not() -> None:
    """`think=False` は明示的に切る。渡されなければテンプレートの既定に任せる（送らない）。"""
    server = _Server(
        completion=_completion("{}"),
        metrics=[_metrics(0, 0, 0, 0)] * 4,
    )
    client = server.client()
    await client.chat(model=MODEL, messages=[{"role": "user", "content": "u"}], think=False)
    await client.chat(model=MODEL, messages=[{"role": "user", "content": "u"}])
    assert server.requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in server.requests[1]


async def test_response_maps_thinking_usage_and_concurrency() -> None:
    server = _Server(
        completion=_completion('{"a": "x"}', reasoning="推論"),
        metrics=[_metrics(2, 1, 100, 3), _metrics(1, 0, 104, 3)],
    )
    response = await server.client().chat(model=MODEL, messages=[{"role": "user", "content": "u"}])
    assert response.message.content == '{"a": "x"}'
    assert response.message.thinking == "推論"
    assert response.prompt_eval_count == 120
    assert response.eval_count == 45
    assert response.requests_running_before == 2
    assert response.requests_waiting_before == 1
    assert response.requests_running_after == 1
    assert response.requests_waiting_after == 0
    # 自分を含めて4件が完了した（他の3件が途中で終わった）
    assert response.requests_finished_during == 4


async def test_reasoning_content_is_accepted_as_thinking() -> None:
    """vLLM の版によって推論本文のキーが `reasoning_content` になる。"""
    completion = _completion("{}", reasoning=None)
    completion["choices"][0]["message"]["reasoning_content"] = "旧名"
    server = _Server(completion=completion)
    response = await server.client().chat(model=MODEL, messages=[{"role": "user", "content": "u"}])
    assert response.message.thinking == "旧名"


async def test_metrics_failure_does_not_break_generation() -> None:
    """`/metrics` が読めなくても生成は続き、同時実行数だけが欠ける。"""
    server = _Server(completion=_completion('{"a": "x"}'), metrics_status=500)
    response = await server.client().chat(model=MODEL, messages=[{"role": "user", "content": "u"}])
    assert response.message.content == '{"a": "x"}'
    assert response.requests_running_before is None
    assert response.requests_finished_during is None


async def test_context_too_small_fails_before_sending() -> None:
    server = _Server(completion=_completion("{}"), max_model_len=8192)
    with pytest.raises(VllmContextTooSmall, match="8192"):
        await server.client().chat(
            model=MODEL, messages=[{"role": "user", "content": "u"}], options={"num_ctx": 32768}
        )
    assert server.requests == []


async def test_unknown_model_fails_before_sending() -> None:
    server = _Server(completion=_completion("{}"))
    with pytest.raises(VllmContextTooSmall, match="にありません"):
        await server.client().chat(
            model="nope", messages=[{"role": "user", "content": "u"}], options={"num_ctx": 8192}
        )
    assert server.requests == []


async def test_images_are_rejected() -> None:
    server = _Server(completion=_completion("{}"))
    with pytest.raises(RuntimeError, match="画像"):
        await server.client().chat(
            model=MODEL, messages=[{"role": "user", "content": "u", "images": [b"x"]}]
        )


async def test_length_cutoff_keeps_thinking_in_the_record() -> None:
    """出力上限で本文が空のとき、**thinking が来歴に残る**。

    例外にすると `chat_json` は thinking を記録できず、上限に達したのか thinking が
    ループしたのかを後から読めない（DSL の生成で実際に起きる形）。
    """
    server = _Server(
        completion=_completion("", reasoning="同じことを繰り返す" * 10, finish="length")
    )
    records: list[LLMCallRecord] = []
    with pytest.raises(AIServiceException, match="空のレスポンス"):
        await chat_json(
            server.client(),
            MODEL,
            system_prompt="s",
            user_message="u",
            response_format=SCHEMA,
            label="テスト",
            recorder=records.append,
        )
    (record,) = records
    assert record.error == "空のレスポンス"
    assert record.thinking == "同じことを繰り返す" * 10
    assert record.timing is not None and record.timing["eval_count"] == 45


async def test_chat_json_records_provider_endpoint_and_concurrency() -> None:
    """来歴に `provider=vllm`、接続先、同時実行数が残る。"""
    server = _Server(
        completion=_completion('{"a": "x"}'),
        metrics=[_metrics(1, 0, 5, 0), _metrics(0, 0, 7, 0)],
    )
    records: list[LLMCallRecord] = []
    parsed = await chat_json(
        server.client(),
        MODEL,
        system_prompt="s",
        user_message="u",
        response_format=SCHEMA,
        label="テスト",
        recorder=records.append,
    )
    assert parsed == {"a": "x"}
    (record,) = records
    assert record.provider == "vllm"
    assert record.endpoint == BASE
    assert record.think is True
    assert record.timing is not None
    assert record.timing["requests_running_before"] == 1
    assert record.timing["requests_running_after"] == 0
    assert record.timing["requests_finished_during"] == 2
    assert record.timing["prompt_eval_count"] == 120


async def test_model_digest_is_the_weight_snapshot() -> None:
    server = _Server(completion=_completion("{}"))
    client = server.client()
    assert await model_digest_of(client, MODEL) == (ROOT, None)


def test_parse_metrics_ignores_other_models() -> None:
    assert parse_metrics(_metrics(3, 2, 10, 4), MODEL) == {
        "running": 3,
        "waiting": 2,
        "finished": 14,
    }
    assert parse_metrics("garbage\n", MODEL) == {}


def test_provider_setting_builds_vllm_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", PROVIDER_VLLM)
    monkeypatch.setattr(settings, "vllm_base_url", BASE + "/")
    monkeypatch.setattr(settings, "vllm_model", MODEL)
    client = make_design_llm_client()
    assert isinstance(client, VllmClient)
    assert provider_of(client) == "vllm"
    assert endpoint_of(client) == BASE
    assert design_llm_model() == MODEL
