"""vLLM（OpenAI 互換 API）への接続アダプタ。

推論サーバを Ollama ではなく vLLM で動かす構成のためのもの。
このクラスは `ollama.AsyncClient.chat` と同じ形（`chat(model=, messages=, format=, options=,
think=)` を受け、`.message.content` を持つ応答を返す）を満たすことだけを責務とする。
継ぎ目は `llm_json.chat_json(client: Any, ...)` で、`ollama.AsyncClient` と同じ位置に立つ。

## Ollama との差の扱い

| Ollama | ここでの扱い |
|---|---|
| `format=<JSON Schema>`（制約デコード）| `response_format: {type: json_schema}` |
| `think=`（トップレベル引数）| `chat_template_kwargs.enable_thinking` |
| `options["temperature"]` / `["seed"]` | そのまま渡す |
| `options["num_ctx"]` | **リクエストでは変えられない**。最初の呼び出しでサーバの `max_model_len` が足りるかを確かめる |
| `message.thinking` | `message.reasoning`（版によって `reasoning_content`）|
| `prompt_eval_count` / `eval_count` | `usage.prompt_tokens` / `usage.completion_tokens` |
| `/api/tags` の digest | `/v1/models` の `root`（重みのスナップショットのパス。HF のリビジョンを含む）|

## 出力上限で本文が空になったとき

他のアダプタは例外にしうるが、**ここでは空の content を返す。** 例外にすると `chat_json` が
thinking を来歴に残せない。上限に達したのか thinking がループしたのかは、thinking の末尾を
読まないと分からない（DSL の生成で実際にこの形になることがある）。
`eval_count` が `max_tokens` と一致することで、上限に達したことは来歴から読める。

## 同時実行数（来歴の `timing`）

vLLM は Web と評価で共有する。生成の中身は temperature 0 なので同時実行の影響を受けないが、
**所要時間は受ける。** 各呼び出しの前後で `/metrics` を読み、次を応答に載せる。

- `requests_running_before` / `requests_waiting_before`：送る直前の他のリクエスト数
- `requests_running_after` / `requests_waiting_after`：応答を受けた直後の数（自分は含まない）
- `requests_finished_during`：呼び出しの間に完了したリクエスト数（自分を含む）

前後のスナップショットなので、途中で始まって途中で終わった他のリクエストは
`requests_finished_during` が2以上になることでしか見えない。
`/metrics` が読めなくても生成は続け、これらの値だけが欠ける。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# `/metrics` を読む上限時間（秒）。生成の全体デッドラインの外で走るので短く切る。
METRICS_TIMEOUT_SECONDS = 5.0

_METRIC_LINE = re.compile(r"^(vllm:[a-z_]+)\{([^}]*)\}\s+([0-9.eE+-]+)$")


@dataclass(frozen=True)
class _Message:
    content: str | None
    thinking: str | None = None


@dataclass(frozen=True)
class VllmResponse:
    """`ollama.AsyncClient.chat` の戻り値のうち、呼び出し側が読む部分だけを持つ。

    整数の属性は `llm_json._timing_of` が来歴の `timing` へ写す。
    """

    message: _Message
    finish_reason: str | None = None
    total_duration: int | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None
    requests_running_before: int | None = None
    requests_waiting_before: int | None = None
    requests_running_after: int | None = None
    requests_waiting_after: int | None = None
    requests_finished_during: int | None = None


class VllmContextTooSmall(RuntimeError):
    """サーバの文脈長が要求（`num_ctx`）に足りない。

    **黙って続けない。** 窓が足りないまま走らせると、切り詰めがどこで起きたか分からないまま
    測定値が動く（`num_ctx` を黙って小さいまま使わないのと同じ理由）。
    """


def parse_metrics(text: str, model: str) -> dict[str, int]:
    """`/metrics` の本文から、`model` の同時実行数と完了数の合計を取り出す。"""
    running = waiting = finished = None
    for line in text.splitlines():
        matched = _METRIC_LINE.match(line.strip())
        if not matched:
            continue
        name, labels, value = matched.groups()
        if f'model_name="{model}"' not in labels:
            continue
        number = int(float(value))
        if name == "vllm:num_requests_running":
            running = number
        elif name == "vllm:num_requests_waiting":
            waiting = number
        elif name == "vllm:request_success_total":
            finished = (finished or 0) + number
    out: dict[str, int] = {}
    if running is not None:
        out["running"] = running
    if waiting is not None:
        out["waiting"] = waiting
    if finished is not None:
        out["finished"] = finished
    return out


class VllmClient:
    """vLLM の OpenAI 互換 API（`/v1/chat/completions`）へ話すクライアント。

    `provider_name` を名乗るので、来歴（`ai_input_design_llm_calls.provider`）に `"vllm"` が残る。
    `base_url` を公開するので、`endpoint` にも接続先が残る。
    """

    provider_name = "vllm"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        max_tokens: int,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._transport = transport
        self._models: dict[str, dict[str, Any]] | None = None

    def _http(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout or self._timeout, transport=self._transport)

    async def _model_entries(self) -> dict[str, dict[str, Any]]:
        if self._models is None:
            async with self._http() as client:
                response = await client.get(f"{self.base_url}/v1/models")
                response.raise_for_status()
                self._models = {m["id"]: m for m in response.json().get("data", [])}
        return self._models

    async def ensure_context_at_least(self, model: str, num_ctx: int) -> int:
        """サーバの `max_model_len` が `num_ctx` 以上あることを確かめ、実際の値を返す。

        Raises:
            VllmContextTooSmall: モデルが無い、または窓が足りない
        """
        models = await self._model_entries()
        entry = models.get(model)
        if entry is None:
            raise VllmContextTooSmall(
                f"モデル {model!r} が {self.base_url} にありません"
                f"（あるのは: {', '.join(sorted(models)) or 'なし'}）"
            )
        length = int(entry.get("max_model_len") or 0)
        if length < num_ctx:
            raise VllmContextTooSmall(
                f"{self.base_url} の {model} の max_model_len が {length} で、要求 {num_ctx} に足りません"
            )
        return length

    async def model_digest(self, model: str) -> str | None:
        """`/v1/models` の `root`（重みのスナップショットのパス）を返す。

        vLLM は digest を持たないが、`root` は HF のリビジョンを含むので、同じモデル名で
        重みが差し替わったことは記録に出る。
        """
        entry = (await self._model_entries()).get(model) or {}
        root = entry.get("root")
        return root if isinstance(root, str) and root.strip() else None

    async def _metrics(self, model: str) -> dict[str, int]:
        """`/metrics` を読む。**例外は投げない**（来歴の記録が測定を壊してはならない）。"""
        try:
            async with self._http(METRICS_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{self.base_url}/metrics")
                response.raise_for_status()
                return parse_metrics(response.text, model)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"vLLM の /metrics を読めなかった: {type(exc).__name__}: {exc}")
            return {}

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        format: dict[str, Any] | None = None,  # noqa: A002 — ollama の引数名に合わせる
        options: dict[str, Any] | None = None,
        think: bool | None = None,
    ) -> VllmResponse:
        """`ollama.AsyncClient.chat` と同じ形で1回呼び出す。

        Raises:
            RuntimeError: 画像つきのメッセージ（vision は Ollama 側に残す）
            VllmContextTooSmall: サーバの窓が `num_ctx` に足りない
            httpx.HTTPError: HTTP 失敗
        """
        if any(m.get("images") for m in messages):
            raise RuntimeError(
                "vllm アダプタは画像を扱いません（vision は OLLAMA_VISION_MODEL 側で処理する）"
            )

        selected = model or self._model
        options = options or {}
        num_ctx = options.get("num_ctx")
        if isinstance(num_ctx, int):
            await self.ensure_context_at_least(selected, num_ctx)

        body: dict[str, Any] = {
            "model": selected,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "max_tokens": self._max_tokens,
        }
        for key in ("temperature", "seed"):
            if options.get(key) is not None:
                body[key] = options[key]
        if think is not None:
            body["chat_template_kwargs"] = {"enable_thinking": bool(think)}
        if format:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": format},
            }

        before = await self._metrics(selected)
        started = time.monotonic()
        async with self._http() as client:
            response = await client.post(f"{self.base_url}/v1/chat/completions", json=body)
            response.raise_for_status()
            payload = response.json()
        total_duration = int((time.monotonic() - started) * 1e9)
        after = await self._metrics(selected)

        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        thinking = message.get("reasoning") or message.get("reasoning_content")
        finish_reason = choice.get("finish_reason")
        usage = payload.get("usage") or {}
        if not content and finish_reason == "length":
            logger.warning(
                f"vLLM: 本文が返る前に max_tokens({self._max_tokens}) に達した"
                f"（thinking {len(thinking or '')} 文字）"
            )

        finished_during = (
            after["finished"] - before["finished"]
            if "finished" in before and "finished" in after
            else None
        )
        return VllmResponse(
            message=_Message(content=content, thinking=thinking),
            finish_reason=finish_reason,
            total_duration=total_duration,
            prompt_eval_count=usage.get("prompt_tokens"),
            eval_count=usage.get("completion_tokens"),
            requests_running_before=before.get("running"),
            requests_waiting_before=before.get("waiting"),
            requests_running_after=after.get("running"),
            requests_waiting_after=after.get("waiting"),
            requests_finished_during=finished_during,
        )
