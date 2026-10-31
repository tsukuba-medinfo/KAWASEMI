"""ai_input 各ステップ共通の Ollama 構造化JSON呼び出しヘルパー。

runbook / compiler / location_drafter はいずれも「client.chat で JSON schema を
指定 → 空content ガード → json.loads → pydantic パース → 例外整形」という同型の
処理を行う。多項目を1回のコールに詰めるとモデルが num_ctx 超過で**空content**を
返すため（location_drafter で4項目再現）、項目を小さく分割して呼ぶ必要がある。
本モジュールはこの「分割 + 構造化JSON + ガード」を1か所に集約する。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import ValidationError

from kawasemi_indexer.exceptions import AIServiceException
from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)

# 多項目を一括で渡すとプロンプトが大きくなり、モデルのコンテキスト超過で空レスポンス
# になる。これを避けるための既定分割数（location_drafter での再現に基づく）。
DEFAULT_CHUNK_SIZE = 4

# 入力＋thinking＋JSON出力が収まるようコンテキスト長を明示し、既定の切り詰めによる
# 空応答を防ぐ。
DEFAULT_OPTIONS: dict[str, Any] = {"temperature": 0, "num_ctx": 8192}


async def chat_json(
    client: Any,
    model: str,
    *,
    system_prompt: str,
    user_message: str,
    response_format: dict[str, Any],
    label: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ollama を1回呼び出し、構造化JSONを dict として返す。

    API例外・空content・JSONパース失敗はいずれも AIServiceException に整形する。

    Args:
        client: ollama.AsyncClient
        model: モデル名
        system_prompt / user_message: チャットメッセージ
        response_format: JSON Schema（pydantic の ``model_json_schema()`` の戻り値）
        label: ログ・例外メッセージ用の処理名（例「手順書生成」）
        options: Ollama options。未指定なら ``DEFAULT_OPTIONS``（num_ctx を含む）。

    Raises:
        AIServiceException: API例外・空content・パース失敗
    """
    try:
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            format=response_format,
            options=options or DEFAULT_OPTIONS,
        )
    except Exception as e:
        logger.error(f"Ollama API呼び出しエラー（{label}）: {e}")
        raise AIServiceException(
            message=f"{label}に失敗しました: {e}",
            details={"model": model, "error": str(e)},
        )

    raw_content = response.message.content
    if not raw_content:
        raise AIServiceException(
            message=f"{label}: モデルから空のレスポンスが返されました",
            details={"model": model},
        )

    try:
        parsed: dict[str, Any] = json.loads(raw_content)
        return parsed
    except json.JSONDecodeError as e:
        logger.error(f"{label}レスポンスのパースエラー: {e}\n{raw_content[:300]}")
        raise AIServiceException(
            message=f"{label}: レスポンスのパースに失敗しました: {e}",
            details={"raw_content": raw_content[:500]},
        )


async def chat_json_chunked(
    client: Any,
    model: str,
    items: Sequence[Any],
    *,
    system_prompt: str,
    build_message: Callable[[list[Any]], str],
    response_format: dict[str, Any],
    parse: Callable[[dict[str, Any]], list[Any]],
    label: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    options: dict[str, Any] | None = None,
) -> list[Any]:
    """``items`` を ``chunk_size`` ずつに分割して :func:`chat_json` を呼び、各 chunk の
    ``parse`` 結果を連結して返す。

    多項目を1コールに詰めると num_ctx 超過で空contentになるため、必ず分割して呼ぶ。
    各 chunk の応答 dict は ``parse`` で list に取り出す。``parse`` 内の
    pydantic ValidationError も AIServiceException に整形する。

    Args:
        items: 処理対象の項目列（dict でも dataclass でもよい）
        build_message: chunk（list）から user メッセージを組み立てる関数
        parse: 応答 dict から結果 list を取り出す関数
        chunk_size: 1コールあたりの最大項目数

    Raises:
        AIServiceException: API例外・空content・パース失敗
    """
    results: list[Any] = []
    for start in range(0, len(items), chunk_size):
        chunk = list(items[start : start + chunk_size])
        data = await chat_json(
            client,
            model,
            system_prompt=system_prompt,
            user_message=build_message(chunk),
            response_format=response_format,
            label=label,
            options=options,
        )
        try:
            results.extend(parse(data))
        except ValidationError as e:
            logger.error(f"{label}レスポンスの検証エラー: {e}")
            raise AIServiceException(
                message=f"{label}: レスポンスのパースに失敗しました: {e}",
                details={"raw_content": json.dumps(data, ensure_ascii=False)[:500]},
            )
    return results
