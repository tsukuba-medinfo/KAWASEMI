"""ai_input 各ステップ共通の Ollama 構造化JSON呼び出しヘルパー。

runbook / compiler / location_drafter はいずれも「client.chat で JSON schema を
指定 → 空content ガード → json.loads → pydantic パース → 例外整形」という同型の
処理を行う。多項目を1回のコールに詰めるとモデルが num_ctx 超過で**空content**を
返すため（location_drafter で4項目再現）、項目を小さく分割して呼ぶ必要がある。
本モジュールはこの「分割 + 構造化JSON + ガード」を1か所に集約する。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from kawasemi.ai_input.llm_provider import DEFAULT_PROVIDER, endpoint_of, provider_of
from kawasemi.ai_input.model_options import load_model_options
from kawasemi.core.config import settings
from kawasemi.core.exception_summary import summarize_exception
from kawasemi.core.exceptions import AIServiceException
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# 多項目を一括で渡すとプロンプトが大きくなり、モデルのコンテキスト超過で空レスポンス
# になる。これを避けるための既定分割数（location_drafter での再現に基づく）。
DEFAULT_CHUNK_SIZE = 4

# 入力＋thinking＋JSON出力が収まるようコンテキスト長を明示し、既定の切り詰めによる
# 空応答を防ぐ。
# `seed` を固定する。**完全な再現は保証できない**（バッチ構成・量子化・
# 並列度で出力は動きうる）が、決定性が上がる範囲では上げる。Ollama は `options.seed` を受ける。
DEFAULT_SEED = 0
DEFAULT_OPTIONS: dict[str, Any] = {"temperature": 0, "num_ctx": 8192, "seed": DEFAULT_SEED}

# **thinking を出すかどうかを明示的に固定する**。
#
# `think` は `options` の中ではなく `/api/chat` の**トップレベル引数**である。渡さないと
# モデルのテンプレートの既定に従うので、**Ollama かモデルが更新されると測定が黙って動く**。
# これが固定する理由で、`num_ctx` や温度とまったく同じ基準である。
#
# thinking は**生成される出力**なので、出るか出ないかで応答そのもの・所要時間・
# `num_ctx` の消費が変わる。thinking に対応したモデルでは、`think` の既定が True の
# ことがあり、そのときは thinking の分だけ応答が数倍遅くなりうる。content の長さは
# ほとんど変わらないので、**出力の外から見ても違いに気づきにくい**。
#
# `prompt_eval_count` も動くが**差はプロンプト依存**である（大きく動くプロンプトも
# あれば、短文ではわずかで向きも一定しない）。したがって
# 「プロンプトが必ず変わる」ではなく「**出力が変わる**」を固定の根拠に置く。
#
# 値は `True`。thinking に対応したモデルが `think=True` をエラーなく受け付けることは
# 確かめてある。thinking の**内容**は捨てずに来歴へ残す
# ——時間と文脈予算を使い出力に影響するものを、監査できない形で流さない。
DEFAULT_THINK = True

# **索引の候補本文や確定済み op 引数を載せる経路は 8192 に収まらない**。
#
# 見積もり（1文字=1トークン換算、チャンク最大構成）:
#
# | 経路 | 固定部（system+schema） | 入力 | 出力予約 | 合計 |
# |---|---|---|---|---|
# | コンパイラ（chunk=4） | 15,393 | 12,264 | 3,000×4 | 39,657 |
# | 資源ドラフタ（chunk=4, snippet 200） | 550+α | 9,590 | 500×4 | 12,140 |
# | 所在ドラフタ（chunk=4） | 776+α | 9,445 | 500×4 | 12,221 |
#
# コンパイラは**固定部だけで 15,393 文字**あるので、刻み幅を1にしても 8192 には収まらない。
# 溢れるとプロンプトの末尾から落ち、症状は「後ろの項目だけ needs_input や捏造が増える」
# という追いにくい形で出る（`clinical_drafter` の予算テストの docstring 参照）。
#
# **臨床的意味の下書きもこちらへ移した**。移した理由は上の3経路
# （プロンプトが 8192 に収まらない）とは別で、**`think=True` の固定で必ず出る
# thinking が予算を食い切る**ためである。入力の上限を入れると 8192 の残りは2千字に満たず、
# thinking の長いモデルでは thinking だけでそれを超えうる。**プロンプトは1文字も変えず、
# 予算だけを移した**（`_TOP_K=3` / `_CHUNK_SIZE=2` / `_MAX_CAND_CHARS=800` は不変）。
#
# **手順書生成もこちらへ移した**。理由は臨床的意味の下書きと同じ thinking の
# 圧迫だが、**症状が違う**——空 content だけでなく**壊れた JSON**でも返る（content が
# 途中で切れて parse 失敗する）。**刻み幅では救えない**：幅を削って空けた予算を
# thinking がそのまま食いうるので（幅を狭めても thinking が短くなるとは限らない）、
# 幅1でも閉じないことがある。よって `RUNBOOK_CHUNK_SIZE` は 3 のまま動かさず、予算だけを移した。
#
# **これで `chat_json_chunked` の呼び出しは5つとも明示的に `LARGE_CONTEXT_OPTIONS` を
# 渡す**（コンパイラ / 資源ドラフタ / 所在ドラフタ / 臨床ドラフタ / 手順書生成）。
# `DEFAULT_OPTIONS` は `chat_json` の既定として残るだけで、**渡し忘れた経路が黙って
# 8192 へ落ちる**受け皿である。だから固定値から外さない。
#
# 32768 にした根拠: 中規模の量子化モデルなら `num_ctx=32768` でロードしても
# **全量が VRAM に載る**（`/api/ps` の `size_vram == size` で確かめられる）。
# 値を1つに揃えてあるのは、
# num_ctx が違うと Ollama がモデルを積み直すため——8192 と 32768 の2種類に留める。
LARGE_CONTEXT_OPTIONS: dict[str, Any] = {"temperature": 0, "num_ctx": 32768, "seed": DEFAULT_SEED}

# **モデルごとの例外**は施設の設定（`site/model_options.yaml`、`model_options.py`）に置き、
# 載ったモデルにだけ options を足す。ファイルが無ければ何も足さない。
#
# 一部のモデルは、長い入力を既定のバッチ幅で処理させると推論サーバが落ちることがあり、
# `num_batch` を下げると回避できる。**足すのはそういう実装上の都合だけにする。**
# プロンプト本文・`temperature`・`seed`・`think`・`num_ctx` は他のモデルと同一で、
# サンプリングの方針も変わらない。**他のモデルの要求は1バイトも変わらない**
# （`test_the_other_models_keep_the_frozen_options` が測る）。
#
# **モデル名で引くのは、これがモデルの実装の都合だからである。** 「長いときだけ下げる」
# のような条件にすると、閾値が新しい調整対象になり、しかもその閾値は落ちるかどうかを
# 見てから決めることになる。


def with_model_overrides(model: str, options: dict[str, Any]) -> dict[str, Any]:
    """モデル固有の例外（`site/model_options.yaml`）を重ねた options を返す。

    **載っていないモデルには何もしない**（同じ dict をそのまま返さず複製もしないので、
    呼び出し側から見た値は不変である）。載っているモデルでも、**渡された options の値は
    上書きしない**——固定した `num_ctx` や `seed` を例外表が黙って動かせる形にしない。
    """
    extra = load_model_options().get(model)
    if not extra:
        return options
    return {**extra, **options}


# **失敗したチャンクを飛ばして続けるが、無条件には続けない**。
#
# チャンクは互いに独立（項目ごとに候補と記述が決まる）なので、1本の空応答で残りを捨てる
# 理由が無い。途中のコールが1本空応答になっただけで、残りの項目が全部未試行になりうる。
# **生成は無駄にならない**（完了済みチャンクは残す設計なので、応答があった項目は
# 保存される）——無駄になるのは**時間**で、長いパスをかけて一部しか埋まらず、
# 残りは押し直しになる。一方、ollama が落ちているときに36コール
# 投げ続けるのも無駄なので、**連続失敗の上限で打ち切る**。
#
# **この定数は測定に効く**（打ち切りの位置が未試行件数を決める＝集計の分母の外に出る
# 項目数が変わる）。値を動かすと測定結果が比較できなくなる。
MAX_CONSECUTIVE_CHUNK_FAILURES = 3


def strict_response_format(model: type[BaseModel], *required: str) -> dict[str, Any]:
    """structured output へ渡すスキーマで、指定したキーを**必須**にする。

    **pydantic の `model_json_schema()` は既定値を持つフィールドを `required` に入れない。**
    Ollama の structured output は渡された JSON スキーマで出力を制約するので、必須でないキーは
    **モデルが省略してよい**ことになる。

    実測: `LLMClinicalDraft.chosen_cand_id` に既定値 `None` があるため、モデルは
    **正しい記述を書きながら `chosen_cand_id` のキーだけを省略**していた。`clinical_drafter` は
    「典拠を選べなかったら記述も採らない」方針なので、**正しい記述が捨てられていた**。

    **pydantic のモデル側は緩いまま（既定値つき）にする。** 必須にすると、モデルがキーを省略した
    応答が `ValidationError` になり**チャンクごと失敗する**（部分結果を残す仕組みが効かなくなる）。
    受けは緩く、要求は厳しくする。

    Args:
        model: 応答の受け皿（`BaseModel`）
        *required: 必須にするフィールド名。**要素の定義（`$defs`）とトップレベルの両方**を見て、
            そのフィールドを持つ定義の `required` へ足す（`{"drafts": [Item]}` のような
            入れ子でも要素側に効かせるため）。スキーマに無い名前は無視する（`required` に
            存在しないキーを入れると Ollama がスキーマを拒否する）

    Raises:
        ValueError: 同じ名前のフィールドが**2つ以上の定義**にあるとき。名前だけでは
            どの定義を厳しくしたいのか決まらないので、**広い方に倒さず落とす**（受け皿へ
            モデルを1つ足した瞬間に、無関係な同名フィールドまで黙って必須化されるのを防ぐ）
    """
    # **pydantic の戻り値をそのまま書き換えない。** pydantic 2.12 は呼ぶたびに新しい dict を
    # 返すので現状は書き換えても外へ漏れないが、**実装依存に賭けない**（将来キャッシュを
    # 返す実装になれば、`required` の追記が pydantic 側へ残って他の呼び出しへ漏れる）。
    schema: dict[str, Any] = json.loads(json.dumps(model.model_json_schema()))
    targets = [*schema.get("$defs", {}).values(), schema]
    for name in required:
        owners = [d for d in targets if name in (d.get("properties") or {})]
        if len(owners) > 1:
            raise ValueError(
                f"必須にするフィールド名 {name!r} が {len(owners)} 個の定義に現れる。"
                "どの定義を厳しくするかが名前だけでは決まらないので中止する"
            )
        for definition in owners:
            current = definition.setdefault("required", [])
            if name not in current:
                current.append(name)
    return schema


@dataclass(frozen=True)
class MergedByLinkId:
    """`link_id` ごとに1件へ畳んだ応答と、**重複が起きた項目**。

    モデルは**同じ項目に2エントリ返すことがある**（まれだが起きる。1エントリ目が
    `cand_id` と記述あり、2エントリ目が両方 null）。dict 内包で組むと**後勝ち**になり、
    **有効な下書きが黙って捨てられる**——典拠の付与率を過小に見せ、その項目を
    「規約文書を引いても意味が決まらなかった項目」へ**誤って入れる**。
    """

    values: dict[str, Any]
    # **重複が起きた項目**。採用した値とは別に持つ（集計はこの項目を分子からも
    # 「規約文書を引いても意味が決まらなかった項目」からも外して別掲できる）。件数だけでは「どの項目か」が追えないので link_id を持つ。
    duplicated_link_ids: tuple[str, ...]


def merge_by_link_id(entries: Sequence[Any], *, value: Callable[[Any], Any]) -> MergedByLinkId:
    """`link_id` ごとに**最初のエントリ**を採り、重複した項目を記録する。

    **なぜ最初を採るか。** 選択肢は3つあった——(a) 最初、(b) 情報のある方（`cand_id` が非 null /
    記述が非空）、(c) 矛盾として第3の状態にする。**(b) は矛盾した応答から「分子が増える方」を
    選ぶことになる**ので採らない（測定の偏りを実装が作る）。(c) が最も正直だが、画面・API・集計に
    状態が1つ増えるので、項目の状態の設計と一緒に扱う。

    **(a) が「正しい答え」を選ぶ規則ではないことは分かっている。** モデルが後から訂正した場合は
    誤る。だからこの関数は**採用と同時に重複を記録する**——集計側が「矛盾した応答だった項目」を
    分子からも「規約文書を引いても意味が決まらなかった項目」からも外して別掲できるようにするのが要点である。規則を documented かつ
    決定的にし、**黙って解決しない**。

    Args:
        entries: 応答の要素列（`link_id` 属性を持つもの）
        value: 要素から採る値を作る関数（経路ごとに違う。臨床は `(cand_id, annotation)` の組、
            所在は `cand_id` だけ）
    """
    values: dict[str, Any] = {}
    duplicated: list[str] = []
    for entry in entries:
        link_id = getattr(entry, "link_id", None)
        if not isinstance(link_id, str):
            continue
        if link_id in values:
            if link_id not in duplicated:
                duplicated.append(link_id)
            continue  # **最初を採る**（後勝ちにしない）
        values[link_id] = value(entry)
    return MergedByLinkId(values=values, duplicated_link_ids=tuple(duplicated))


class PartialResultException(AIServiceException):
    """一部のチャンクが失敗・未試行のまま終わったが、**結果のあるチャンクがある**。

    `AIServiceException` のサブクラスなので、**部分結果を扱わない呼び出し側は従来どおり
    失敗として扱える**（既存の `except AIServiceException` はそのまま効く）。ただし
    **失敗したチャンクを飛ばして続けるので、扱わない選択は「より多く生成してより多く捨てる」
    ことを意味する**。ai_input の4経路はすべて扱う。

    **会計は3状態で、恒等式が閉じる**。

    ====================  ==========================================================
    `answered_items`      応答が返って `parse` も通ったチャンクの項目数
    `failed_items`        **送ったが結果にならなかった**項目数（空応答・JSON 崩れ・
                          スキーマ不一致・1コールのタイムアウト・打ち切られたチャンク）
    `unattempted_items`   **送っていない**項目数（連続失敗の上限で打ち切った残り、
                          全体デッドラインで到達しなかった残り）
    ====================  ==========================================================

    `answered_items + failed_items + unattempted_items == len(items)`（`total_items`）。
    **この恒等式が閉じないと、どの状態にも入らない項目が黙って消える。**

    `unanswered_items` は `failed_items + unattempted_items`（**応答が無かった全部**）で、
    既存の呼び出し側の互換のために残している。

    **`unanswered_items` を無視して `partial` だけを使ってはいけない。** 応答が返らなかった
    項目を「試したが結果が無かった」と同じに扱うと、「規約文書を引いても
    意味が決まらなかった項目」へ未試行が混入する。

    どの件数も**チャンク粒度の下限値**である。応答が返って parse も通った**チャンク**の
    項目数を数えているので、**チャンクが返ったが LLM が一部の項目を落とした場合、落ちた
    項目は「応答あり」に入る**。項目単位の未試行が必要な呼び出し側は、自分が渡した項目と
    返ってきた結果を**同一性で突き合わせて数え直すこと**（`clinical_drafter` はそうしている）。

    打ち切られたチャンクの項目は「応答あり」に入れない（呼び出しの直前に数えると「試した」に
    入ってしまう。実装時に踏んだ誤りで、テストが捕まえた）。
    """

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        partial: list[Any],
        answered_items: int,
        failed_items: int,
        unattempted_items: int,
    ) -> None:
        super().__init__(message=message, details=details)
        self.partial = partial
        self.answered_items = answered_items
        self.failed_items = failed_items
        self.unattempted_items = unattempted_items
        # 既存の呼び出し側との互換。「応答が無かった全部」＝失敗＋未試行。
        self.unanswered_items = failed_items + unattempted_items


@dataclass(frozen=True)
class LLMCallRecord:
    """LLM 呼び出し1回の来歴。

    実行ロールが evidence trace に残している `llm{model, options, prompt, raw_output}`
    と同じ粒度である。設計ロールだけがこれを保存する。**理由は入力の性質の違いで、
    設計ロールの入力は規約文書・スキーマ定義・項目名であって患者データを含まない**
    （論証は `docs/design_llm_provenance.md`）。

    `raw_output` は失敗時も記録する。パースできなかった応答そのものが、モデルが
    何を返したかの唯一の手掛かりになるためである。

    chunk_index / item_count は分割呼び出し（`chat_json_chunked`）の何回目かを表す。
    どのチャンクでどの項目を渡したかが分からないと、記述と入力を突き合わせられない。
    """

    label: str
    model: str
    options: dict[str, Any]
    system_prompt: str
    user_message: str
    raw_output: str | None
    error: str | None = None
    chunk_index: int = 0
    item_count: int = 0
    # 推論プロバイダ。**モデル名だけでは実装が特定できない。** `ollama_model` は
    # 環境変数で上書きでき、同じ名前を別のエンジンが名乗れる。既定を "ollama" にしてある
    # のは移行前の記録と揃えるためで、値は `provider_of` が client から取る。
    provider: str = DEFAULT_PROVIDER

    # **thinking の設定と中身**。`think` は渡した値、`thinking` はモデルが
    # 返した推論本文である。**両方要る**——`think=True` でもモデルが何も考えないことは
    # ありうるので、`thinking` が空であることだけでは「切っていた」と区別できない。
    think: bool | None = None
    thinking: str | None = None

    # **接続先**。所要時間は**マシン依存**なので、時間だけ残しても
    # 比較できない（別マシン・別モデルの実測は当てはまらない）。どこへ投げたかを記録に持たせる。
    endpoint: str | None = None

    # **所要時間**。自動生成にかかった時間（1項目あたりの秒数を、使ったモデルごとに比べる）の唯一の出所である。
    # **生成の瞬間にしか捕まえられない**——行は生成後に1度だけ作られるので
    # `created_at`/`updated_at` の差からも出せない。
    #
    # JSON で持つのは、Ollama が返す内訳（`total_duration` / `load_duration` /
    # `eval_count` / `eval_duration` / `prompt_eval_count` / `prompt_eval_duration`、
    # いずれもナノ秒）を**列を足さずに**そのまま残すためである。
    # **`load_duration` が別に取れるのが効く**——初回コールのモデルロードを
    # 生成時間から差し引けないと、1本目のモデルだけ遅く見える。
    # `elapsed_ms` はクライアント側の壁時計（ネットワークを含む）で、サーバ側の値が
    # 取れないプロバイダでも残る。
    timing: dict[str, Any] | None = None


# 来歴の受け取り口。**設計ロールだけが渡す。** 実行ロール（executor）は llm_json を
# 使わず自分で evidence trace へ書くので、この経路で PHI が混ざることはない。
LLMCallRecorder = Callable[[LLMCallRecord], None]


# Ollama が `ChatResponse` で返す計測値（いずれもナノ秒 or 件数）。**列を足さずに残す**。
_SERVER_TIMING_FIELDS = (
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
    # vLLM の同時実行数。Web と評価で推論サーバを共有するので、所要時間が
    # 他のリクエストに汚染されたかを後から判別できるようにする。Ollama の応答には無い。
    "requests_running_before",
    "requests_waiting_before",
    "requests_running_after",
    "requests_waiting_after",
    "requests_finished_during",
)


def _timing_of(started: float, response: Any) -> dict[str, Any]:
    """1コールの所要時間を組み立てる。

    `elapsed_ms` はクライアント側の壁時計で、**応答が返らなかった場合も残る**
    （中断・API例外でも「何秒待ったか」は記録の一部である）。

    サーバ側の内訳は `ChatResponse` が既に持っているので、**捨てずにそのまま写す**。
    **`load_duration` が別に取れるのが効く**——初回コールのモデルロードを生成時間から
    差し引けないと、複数のモデルを逐次で回したときに1本目だけ遅く見える（所要時間をモデルどうしで
    比べるとき、ロード時間が混ざるとモデルの速度ではなく実行順を測ることになる）。

    **この関数は例外を投げない。** 来歴の検証が測定を壊してはならない
    （`llm_provider.provider_of` の docstring にある実測と同じ理由）。
    """
    timing: dict[str, Any] = {"elapsed_ms": int((time.monotonic() - started) * 1000)}
    for field in _SERVER_TIMING_FIELDS:
        try:
            value = getattr(response, field, None)
        except Exception:  # noqa: BLE001 - 取れないことは失敗ではない
            continue
        if isinstance(value, int):
            timing[field] = value
    return timing


async def chat_json(
    client: Any,
    model: str,
    *,
    system_prompt: str,
    user_message: str,
    response_format: dict[str, Any],
    label: str,
    options: dict[str, Any] | None = None,
    recorder: LLMCallRecorder | None = None,
    chunk_index: int = 0,
    item_count: int = 0,
) -> dict[str, Any]:
    """LLM を1回呼び出し、構造化JSONを dict として返す。

    **client は Ollama 限定ではない**。`chat(model, messages, format, options)` を持ち
    `.message.content` を返すものであればよく、ここが別プロバイダのアダプタを挿す継ぎ目である。

    API例外・空content・JSONパース失敗はいずれも AIServiceException に整形する。

    Args:
        client: `.chat(...)` と `.message.content` を持つ client（`ollama.AsyncClient` か
            プロバイダのアダプタ）
        model: モデル名
        system_prompt / user_message: チャットメッセージ
        response_format: JSON Schema（pydantic の ``model_json_schema()`` の戻り値）
        label: ログ・例外メッセージ用の処理名（例「手順書生成」）
        options: Ollama options。未指定なら ``DEFAULT_OPTIONS``（num_ctx を含む）。
        recorder: 来歴の受け取り口。**設計ロールだけが渡す。** 渡された
            場合はプロンプト全文と生応答を記録する（成功・失敗とも）。既定の None では
            何も残さないので、実行ロールの PHI 保護方針は変わらない。
        chunk_index / item_count: 分割呼び出しの何回目・何項目か（来歴の突き合わせ用）。

    Raises:
        AIServiceException: API例外・空content・パース失敗
    """
    effective_options = with_model_overrides(model, options or DEFAULT_OPTIONS)
    provider = provider_of(client)
    endpoint = endpoint_of(client)
    started = time.monotonic()

    def _record(
        raw_output: str | None,
        error: str | None,
        thinking: str | None = None,
        response: Any = None,
    ) -> None:
        if recorder is None:
            return
        recorder(
            LLMCallRecord(
                label=label,
                model=model,
                options=dict(effective_options),
                system_prompt=system_prompt,
                user_message=user_message,
                raw_output=raw_output,
                error=error,
                chunk_index=chunk_index,
                item_count=item_count,
                provider=provider,
                think=DEFAULT_THINK,
                thinking=thinking,
                endpoint=endpoint,
                timing=_timing_of(started, response),
            )
        )

    try:
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            format=response_format,
            options=effective_options,
            # **thinking を明示的に固定する**。渡さないとモデルの既定に従い、
            # Ollama やテンプレートの更新で測定が黙って動く。値の根拠は `DEFAULT_THINK`。
            think=DEFAULT_THINK,
        )
    except asyncio.CancelledError:
        # **全体デッドラインで中断されても来歴を1件残す**。
        # `CancelledError` は `BaseException` なので下の `except Exception` に入らず、
        # **送信したのに証拠がどこにも残らない**状態だった（会計では `failed` に数える）。
        _record(None, "全体デッドラインで中断（送信済み・応答なし）")
        raise
    except Exception as e:
        # httpx.ReadTimeout 等は str(e) が空になり原因が無言化するため型名で補う
        detail = summarize_exception(e)
        logger.error(f"Ollama API呼び出しエラー（{label}）: {detail}", exc_info=e)
        _record(None, detail)
        raise AIServiceException(
            message=f"{label}に失敗しました: {detail}",
            details={"model": model, "error": detail},
        )

    raw_content = response.message.content
    # **thinking は content と別のフィールドで返る**。JSON は汚れないが、
    # 時間と `num_ctx` を消費し出力に影響するので捨てずに残す。
    thinking = getattr(response.message, "thinking", None)
    if not raw_content:
        _record(raw_content, "空のレスポンス", thinking, response)
        raise AIServiceException(
            message=f"{label}: モデルから空のレスポンスが返されました",
            details={"model": model},
        )

    try:
        parsed: dict[str, Any] = json.loads(raw_content)
    except json.JSONDecodeError as e:
        # PHI 保護: LLM 生応答は PHI を含みうるためログ・例外に載せず、
        # 長さとエラー内容のみ記録する。**この方針は変えない。** 設計ロールの来歴は
        # ログや例外ではなく DB へ記録し、その保存経路だけを別に持つ。
        detail = str(e) or type(e).__name__  # 空メッセージ対策
        logger.error(f"{label}レスポンスのパースエラー: {detail} (raw_len={len(raw_content)})")
        _record(raw_content, detail, thinking, response)
        raise AIServiceException(
            message=f"{label}: レスポンスのパースに失敗しました: {detail}",
            details={"model": model, "error": detail},
        )
    if not isinstance(parsed, dict):
        # **トップレベルが dict でない応答は「JSON の崩れ」として失敗にする**。注釈は `dict[str, Any]` だが実行時の検査は
        # 無く、モデルが配列を返すと呼び出し側の `parse` が `TypeError` を上げて
        # **完了済みチャンクごと落ちていた**。`chat_json` は JSON の崩れを失敗にする境界なので、
        # ここが正しい位置である。
        logger.error(f"{label}レスポンスの型エラー: {type(parsed).__name__}（dict でない）")
        _record(raw_content, f"型エラー: {type(parsed).__name__}", thinking, response)
        raise AIServiceException(
            message=f"{label}: レスポンスが JSON オブジェクトではありません",
            details={"model": model, "error": f"top_level={type(parsed).__name__}"},
        )
    _record(raw_content, None, thinking, response)
    return parsed


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
    recorder: LLMCallRecorder | None = None,
) -> list[Any]:
    """``items`` を ``chunk_size`` ずつに分割して :func:`chat_json` を呼び、各 chunk の
    ``parse`` 結果を連結して返す。

    多項目を1コールに詰めると num_ctx 超過で空contentになるため、必ず分割して呼ぶ。
    各 chunk の応答 dict は ``parse`` で list に取り出す。``parse`` 内の
    pydantic ValidationError も AIServiceException に整形する。

    **1チャンクの失敗で残りを捨てない**。失敗したチャンクは飛ばして次を試し、
    **連続失敗が :data:`MAX_CONSECUTIVE_CHUNK_FAILURES` に達したら打ち切る**。失敗した
    チャンクの項目は「送ったが結果にならなかった」、打ち切った残りは「送っていない」として
    別に数える（:class:`PartialResultException` の3状態）。

    Args:
        items: 処理対象の項目列（dict でも dataclass でもよい）
        build_message: chunk（list）から user メッセージを組み立てる関数
        parse: 応答 dict から結果 list を取り出す関数
        chunk_size: 1コールあたりの最大項目数
        recorder: 来歴の受け取り口。各チャンクの呼び出しごとに1件記録する

    Raises:
        PartialResultException: 結果のあるチャンクがあり、かつ失敗・未試行のチャンクもある
        AIServiceException: 結果が1件も無い（モデルかプロンプトが壊れている示唆）
    """

    # **完了済みチャンクの結果を関数の外に置く**。`_run()` のローカルに置くと
    # `asyncio.wait_for` のキャンセルで全部消え、**37コール分の生成が1つも残らない**。
    results: list[Any] = []
    answered = 0
    # **送信前（`build_message`）の失敗**。
    # `failed` には数えない（送っていない）が、**完了済みチャンクと来歴は捨てない**。
    pre_send_failure: dict[str, Any] | None = None
    # **送ったが結果にならなかった項目数**。未試行と混ぜると、呼び出し側は
    # 「押し直せば埋まる」項目と「同じ入力では埋まらない」項目を区別できない。
    failed = 0
    failures: list[dict[str, Any]] = []
    # **送信中のチャンク**。全体デッドラインで `wait_for` が
    # キャンセルすると、**そのチャンクは送信済みで応答が返らなかった**——`failed`（送ったが
    # 結果にならなかった）であって `unattempted`（送っていない）ではない。差で導出していたので
    # **送信済みの項目が「送っていない」に入り、集計の分母から静かに消えていた**。
    in_flight: dict[str, Any] | None = None
    # **最初の失敗の details**（原因ごとの情報。パース失敗の raw_len など）。全チャンクが
    # 失敗したときに、原因を名指しできる情報を捨てないために持つ。
    first_details: dict[str, Any] | None = None
    aborted = False

    total_chunks = -(-len(items) // chunk_size) if chunk_size > 0 else 0

    def _accounting() -> dict[str, int]:
        """3状態の内訳。**恒等式が閉じることを caller 側の assert ではなくここで担保する。**"""
        unattempted = max(0, len(items) - answered - failed)
        return {
            "total_items": len(items),
            "answered_items": answered,
            "failed_items": failed,
            "unattempted_items": unattempted,
            # 既存の呼び出し側との互換（応答が無かった全部）。
            "unanswered_items": failed + unattempted,
            "completed": len(results),
        }

    async def _run() -> list[Any]:
        nonlocal answered, failed, aborted, first_details, in_flight, pre_send_failure
        consecutive = 0
        for chunk_index, start in enumerate(range(0, len(items), chunk_size)):
            chunk = list(items[start : start + chunk_size])
            # **`build_message` は送信前なので `failed`（送った）に数えない**。広い `except` の中に置いていたので、`build_message` のバグで
            # **1件も送っていない項目が `failed` に数えられ**、理由文が「レスポンスの解釈に失敗」と
            # モデルを誤って名指しし、連続失敗の上限が決定的なコードのバグを「押し直せば埋まる
            # 未試行」に化けさせていた（実測: `client.chat` の呼び出し 0 回で `failed_items = 9`）。
            #
            # **ただし素のまま伝播させてもいけない**。2つ目以降のチャンクで
            # 落ちると `wait_for` もルートの `except AIServiceException` も抜けて
            # **完了済みチャンクと来歴が両方消えて 500** になる——チャンクの失敗で全部を捨てる形の再発である。
            # ここで受けてループを止め、**部分結果があれば `PartialResultException`、無ければ
            # `AIServiceException`** として上げる（会計では**未試行**に落ちる）。
            try:
                user_message = build_message(chunk)
            except Exception as e:  # noqa: BLE001 - `build_message` は呼び出し側の関数
                logger.error(f"{label}: 入力の組み立てに失敗（{type(e).__name__}）")
                pre_send_failure = {
                    "chunk_index": chunk_index,
                    "item_count": len(chunk),
                    "reason": (
                        f"{label}: 入力の組み立てに失敗しました（{type(e).__name__}）"
                        "——モデルの応答ではなく呼び出し側のコードの失敗です"
                    ),
                }
                return results
            # **送る直前に記録する**（送ったことの証拠。デッドラインで中断されたら
            # `TimeoutError` 側が `failed` へ繰り入れる）
            in_flight = {"chunk_index": chunk_index, "item_count": len(chunk)}
            try:
                data = await chat_json(
                    client,
                    model,
                    system_prompt=system_prompt,
                    user_message=user_message,
                    response_format=response_format,
                    label=label,
                    options=options,
                    recorder=recorder,
                    chunk_index=chunk_index,
                    item_count=len(chunk),
                )
                parsed = parse(data)
            except ValidationError as e:  # noqa: F841 - 下で errors() を使う
                # PHI 保護: LLM 生応答/入力値を載せず、失敗件数のみ記録する。
                logger.error(
                    f"{label}レスポンスの検証エラー: {len(e.errors())}件のフィールド検証失敗"
                )
                reason = f"{label}: レスポンスの検証に失敗しました"
                details: dict[str, Any] = {"error_count": len(e.errors())}
            except AIServiceException as e:
                # 空応答・JSON の崩れ・1コールのタイムアウト・API 例外。**ここで抜けない**。
                # チャンクは互いに独立なので、次を試す理由がある。
                reason = e.message
                details = dict(e.details or {})
            except Exception as e:  # noqa: BLE001 - 下の理由で意図的に広く受ける
                # **`parse` が何を上げるかは列挙できない**。`parse` は
                # 呼び出し側が渡す関数で、モデルがトップレベルに配列を返すと
                # `GeneratedPlans(**d)` が `TypeError` になる。以前はそれが `wait_for` を
                # 抜けて生のまま伝播し、**完了済みチャンクも来歴も残らず 500 になった**
                # （ルートの `except AIServiceException` に入らないため）。
                # **完了済みチャンクを捨てないことを優先**し、チャンクの失敗として記録する。
                logger.error(f"{label}: レスポンスの解釈に失敗（{type(e).__name__}）")
                reason = f"{label}: レスポンスの解釈に失敗しました（{type(e).__name__}）"
                details = {"error_type": type(e).__name__}
            else:
                # 到達不能な防御（この直後に `await` は無いので、次の周の代入で
                # 必ず上書きされる。**無検証の防御として残す**）
                in_flight = None
                results.extend(parsed)
                # **応答が返って parse も通った項目だけを数える**。呼び出しの
                # 直前に数えると、**途中で打ち切られたチャンクの項目が「試した」に入る**
                # （テストが実際にこの誤りを捕まえた）。
                answered += len(chunk)
                consecutive = 0  # **成功で連続失敗を 0 に戻す**（散発的な失敗で打ち切らない）
                continue

            # こちらも到達不能な防御（ループ先頭で毎周代入し直すので、
            # `chat_json` 以外に `await` が無い限り古い値は残らない）
            in_flight = None
            failed += len(chunk)
            consecutive += 1
            if first_details is None:
                first_details = details
            failures.append(
                {"chunk_index": chunk_index, "item_count": len(chunk), "reason": reason}
            )
            logger.warning(
                f"{label}: チャンク{chunk_index + 1}/{total_chunks}が失敗（{reason}）。"
                f"{len(chunk)}項目を飛ばして次を試す（連続失敗 {consecutive}回）"
            )
            if consecutive >= MAX_CONSECUTIVE_CHUNK_FAILURES:
                # **無条件には続けない**。ollama が落ちているときに残りを
                # 投げ続けるのは無駄なので、ここで止めて**残りは未試行として数える**。
                aborted = True
                logger.error(
                    f"{label}: 連続{consecutive}回失敗したので打ち切った"
                    f"（上限 {MAX_CONSECUTIVE_CHUNK_FAILURES}回）"
                )
                return results
        return results

    # 全体デッドライン: 1コール=ollama_timeout に対し、チャンク数に依らない
    # 総上限を設け、N チャンクで無制限に接続を保持し続けるのを防ぐ。
    total_timeout = settings.llm_total_timeout
    try:
        await asyncio.wait_for(_run(), timeout=total_timeout)
    except TimeoutError:  # 3.11+ では asyncio.TimeoutError は builtin TimeoutError の別名
        # **完了済みチャンクの結果を捨てない**。数十項目の一括生成は1コール
        # 数十秒で数十コールになるので、**どう実装しても既定の 600 秒には収まらない**。
        # ここで捨てると、一連の作業（対象項目の臨床的意味を下書きする）の最初の一歩が
        # 何度やっても完走しない。
        #
        # **「試していない項目」を「試して典拠が無かった項目」と混ぜないことが要点**である。
        # 混ぜると未試行が「規約文書を引いても意味が決まらなかった項目」に入り、
        # 分子が汚染される。呼び出し側が区別できるように、部分結果と
        # 内訳を例外に載せて渡す。
        if in_flight is not None:
            # **中断されたチャンクは「送った」側に数える**。
            # 生成時間を消費して応答が返らなかったので、押し直せば埋まりうる項目である。
            failed += int(in_flight["item_count"])
            failures.append(
                {
                    **in_flight,
                    "reason": f"{label}: 全体デッドラインで中断されました（送信済み・応答なし）",
                }
            )
        acc = _accounting()
        logger.error(
            f"{label}: 全チャンク処理が全体デッドライン({total_timeout}s)を超過しました"
            f"（応答あり {acc['answered_items']}項目 / 失敗 {acc['failed_items']}項目 / "
            f"未試行 {acc['unattempted_items']}項目）"
        )
        # **キーの集合を2つの生成箇所で揃える**（片方に無いと
        # `details["aborted_on_consecutive_failures"]` を読む呼び出し側が KeyError になる）
        details_out: dict[str, Any] = {
            "model": model,
            "timeout": total_timeout,
            "aborted_on_consecutive_failures": aborted,
            "max_consecutive_chunk_failures": MAX_CONSECUTIVE_CHUNK_FAILURES,
            "chunk_failures": list(failures),
            **acc,
        }
        if not results:
            # **1件も結果が無ければ素の失敗**。ループ完走側にだけ置いていた
            # ガードがここに無かったので、**以前は 500 だったものが 200 + `success: True` + 0件**
            # になっていた（4経路が `e.partial` を採るようになったため）。docstring の
            # `Raises` もそう書いてあったのに、実装がデッドライン側だけ違っていた。
            raise AIServiceException(
                message=f"{label}: 処理が全体の制限時間を超過しました（結果は1件もありません）",
                details=details_out,
            )
        raise PartialResultException(
            message=f"{label}: 処理が全体の制限時間を超過しました",
            details=details_out,
            partial=list(results),
            answered_items=acc["answered_items"],
            failed_items=acc["failed_items"],
            unattempted_items=acc["unattempted_items"],
        )

    acc = _accounting()
    if not failures and pre_send_failure is None:
        # 全チャンクが応答した正常系。**ここで例外を作らない**（未試行が発火しないこと自体を
        # テストで固定している）。
        return results
    # **送信前の失敗を内訳へ入れる**（`failed` には数えない＝会計では未試行）。理由文で
    # **呼び出し側のコードの失敗**であることを名指しする（モデルのせいだと読めてはいけない）。
    all_failures = [*failures, *([pre_send_failure] if pre_send_failure else [])]
    if not results:
        # **1件も完了していないときは素の失敗にする。** 部分結果が空なのに
        # `PartialResultException` を投げると、呼び出し側は「途中まで成功した」と読む。
        # **モデルかプロンプトが壊れている**示唆なので、途中まで採る意味が無い。
        # ただし**内訳は details に残す**（未試行が何件あるかは押し直す判断に効く）。
        #
        # **メッセージは最初の失敗の原因をそのまま使う**（「すべて失敗しました」に置き換えない）。
        # 1チャンクしか無い経路（compiler / runbook の通常ケース）では、画面に出る文字列が
        # 「空応答」「JSON の崩れ」から**原因を名指ししない文へ退化する**。
        logger.error(
            f"{label}: 全チャンクが失敗しました（失敗 {acc['failed_items']}項目 / "
            f"未試行 {acc['unattempted_items']}項目）"
        )
        raise AIServiceException(
            message=str(all_failures[0]["reason"]),
            details={
                **(first_details or {}),
                "model": model,
                # **打ち切りの事実はここにも残す**。全チャンク失敗は
                # **連続失敗の上限が最も効く経路**（ollama が落ちている）なのに、`details` に
                # 打ち切りの記録が無く `details["aborted_on_consecutive_failures"]` が
                # KeyError になっていた。打ち切りの記録はこの経路でこそ要る。
                "aborted_on_consecutive_failures": aborted,
                "max_consecutive_chunk_failures": MAX_CONSECUTIVE_CHUNK_FAILURES,
                "chunk_failures": list(all_failures),
                **acc,
            },
        )
    # **失敗したチャンクがあれば、ループを最後まで回っても部分結果として上げる**。
    # 成功として返すと、呼び出し側は落ちた項目を「試して結果が無かった」と数える。
    if pre_send_failure is not None:
        reason = "入力の組み立てに失敗しました（呼び出し側のコード）"
    elif aborted:
        reason = "連続失敗の上限で打ち切りました"
    else:
        reason = "一部のチャンクが失敗しました"
    logger.error(
        f"{label}: {reason}（応答あり {acc['answered_items']}項目 / "
        f"失敗 {acc['failed_items']}項目 / 未試行 {acc['unattempted_items']}項目）"
    )
    raise PartialResultException(
        message=f"{label}: {reason}",
        details={
            "model": model,
            "aborted_on_consecutive_failures": aborted,
            "max_consecutive_chunk_failures": MAX_CONSECUTIVE_CHUNK_FAILURES,
            "chunk_failures": list(all_failures),
            **acc,
        },
        partial=list(results),
        answered_items=acc["answered_items"],
        failed_items=acc["failed_items"],
        unattempted_items=acc["unattempted_items"],
    )
