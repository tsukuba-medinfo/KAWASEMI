"""ステップ2: データ取得手順書（runbook）の生成

ステップ1の2役割（臨床的コンテキストアノテーション＋データ所在）を読み込み、Ollamaを使って
項目ごとに「どうデータを取得するか」を説明する人間可読な順序付きステップ列を生成する。

従来のステップ2（local_sql 等を直接生成）とは異なり、ここでの出力は実行可能コードではなく、
人がレビュー・編集する手順書である。確認済み手順書は将来ステップ3で InputPlan へコンパイルされる。
"""

from __future__ import annotations

from typing import Any

from kawasemi.ai_input.design_provenance import DesignLlmRecorder
from kawasemi.ai_input.llm_json import (
    LARGE_CONTEXT_OPTIONS,
    LLMCallRecorder,
    PartialResultException,
    chat_json_chunked,
    strict_response_format,
)
from kawasemi.ai_input.llm_provider import design_llm_model, make_design_llm_client
from kawasemi.ai_input.schemas import GeneratedRunbookItem, GeneratedRunbooks
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# structured output で**モデルに省略を許さないキー**（`compiler._REQUIRED_PLAN_KEYS` と同型）。
#
# `model_json_schema()` は既定値を持つフィールドを `required` に入れないので、structured
# output は**モデルにそのキーの省略を許す**。`GeneratedRunbookItem.steps` は既定値つき
# なので、モデルが `{"link_id": "x"}` を返すと**手順書0ステップが黙って通る**。
#
# **`RunbookStep` の各キーは入れない。** `description` / `action_type` / `data_hint` は
# 空文字や null で返るより省略のほうが後段で扱いやすく、`steps` を必須にすれば
# 「0ステップが黙って通る」形は閉じる。**op に依らない一様なキーだけを必須にする**
# という線引きに従う。
_REQUIRED_RUNBOOK_KEYS = ("steps",)

RUNBOOK_SYSTEM_PROMPT = """あなたはFHIR準拠のEDC（電子データ収集）システムのAIアシスタントです。
各入力項目について、「どのデータをどこからどうやって取得・加工して値を埋めるか」を、人間の担当者が
読んで実行できる**手順書（runbook）**として作成してください。コードやSQLを直接書くのではなく、
順序立てた手順の説明を作ります。

各項目には次が与えられます:
- 臨床的コンテキスト: いつ・どの検体を・どの条件で採用するかなどの医学ロジック（施設非依存）
- データ所在: データが物理的にどこにあるか（接続先・表・カラム等、施設固有）
- ソース種別: fhir / local_db / external_db / llm / manual

各項目につき steps（順序付きステップの配列）を生成してください。各ステップ:
- order: 1始まりの実行順（整数）
- title: ステップの短い見出し（例「起点イベントを特定する」）
- description: 担当者が実行する具体的手順の説明（自由文）。臨床的コンテキストとデータ所在を
  突き合わせ、起点・時間窓・検体優先順・交絡条件・単位変換などを順を追って書く。
- action_type: そのステップの種類を次から1つ選ぶタグ（任意）:
  fetch（データ取得） / select（取得結果から1件を選ぶ・絞り込む。所在候補の選択ではない）
  / transform（変換・単位換算・コード変換） / llm（自由記述テキストからの抽出をLLMに任せる）
  / manual（人手確認） / condition（どちらの経路を採るかの判定）
  / branch（判定に応じて取得元・値を選ぶ分岐）
  タグは手順の意味を表す。実装がどうなるか（SQL 1本で済むかどうか）は問わない。
- data_hint: 参照するデータ源・テーブル・カラム・クエリ例などの補足（任意）

ガイドライン:
- 単一の処理にまとめず、起点特定→絞り込み→抽出→変換のように手順を分解する。
- データ所在が空でも、臨床的コンテキストから論理的な取得手順を記述する。
- manual 項目は「どの資料を見て手入力するか」を手順として書く。
- 必ず全ての項目を runbooks 配列に含めること。

条件分岐の書き方:
- **取得元が患者の文脈で変わる**とき（例「入院症例は部門システム、外来症例は電子カルテ」）は、
  ①condition のステップで**何を見てどちらと判定するか**を書き、②branch のステップで
  **判定ごとにどちらから取るか**を書く。取得そのものは fetch のステップに分ける。
- **優先順の候補が複数ある**とき（例「FHIR にあればそれを使い、無ければ院内DBから取る」）も
  同じ形で書く。①condition で「第1候補が空かどうかを見る」、②branch で「空なら第2候補から
  取る」と書く。
- **判定の根拠は臨床的コンテキストから採る。** データ所在に書かれていない条件を推測で作らない。
  判定できる材料が無いなら、condition の description に「どの情報が必要か」を書いて manual に
  する。
- title と description にコードや SQL を書かない（この方針は分岐でも変わらない）。
  「どちらを採るか」を日本語で書く。クエリ例を添えるなら data_hint に置く。
- **「行があれば1・無ければ2」のような有無の判定も condition + branch で書く。** それが
  ステップ3で SQL 1本になるか2つの op になるかは、手順書の書き方を変えない。"""


# 1コールに詰める項目数。既定（`llm_json.DEFAULT_CHUNK_SIZE` = 4）より小さくしているのは、
# system プロンプトに条件分岐の書き方を足して固定部が 870 → 1599 文字（約1.8倍）に
# なり、臨床的意味とデータ所在の自由文が長い項目を4件詰めると num_ctx 8192 の予算（**文字数
# での見積もり**。トークン数の上界ではない）に収まらなくなったため
# （`tests/ai_input/test_runbook_branch.py::test_generation_prompt_fits_the_num_ctx_budget`
# が実測している）。
#
# 溢れたときの症状は2通り:
# - モデルが空 content を返す → `llm_json.chat_json` が `AIServiceException` を投げ、
#   一括生成のリクエストが「モデルから空のレスポンスが返されました」で失敗する（500）。
#   **黙って落ちない。**
# - 応答は返るが一部の項目を落とす → 一括生成（`routes.py` の `generate_runbooks`）は
#   落とされた項目を `continue` で飛ばすので、**その項目だけ手順書が未生成のまま残る**
#   （既存行があればそのまま。応答の `generated` 件数が要求より少なくなるだけ）。
#   **例外にはならないが痕跡は残る**: ウィザードは未生成の項目を空欄として並べ、
#   「この項目だけ再生成」で回復できる。刻み幅を下げるのは、この2番目を減らすため。
RUNBOOK_CHUNK_SIZE = 3


class AIRunbookGenerator:
    """ステップ2: 2役割入力から手順書（順序付きステップ）を生成するクラス"""

    def __init__(self) -> None:
        # プロバイダは設定で決まる。Ollama（既定）または vLLM。
        # `chat_json` が読むのは
        # `.message.content` だけなので、契約はそこに閉じている。
        self._client = make_design_llm_client()

    def _build_user_message(self, items: list[dict[str, Any]]) -> str:
        lines = ["以下の各入力項目について手順書（steps）を生成してください:\n"]
        for d in items:
            lines.append(f"### linkId: {d.get('link_id')}")
            lines.append(f"  項目名: {d.get('item_text') or '（未設定）'}")
            lines.append(f"  ソース種別: {d.get('source_category') or 'manual'}")
            if d.get("annotation"):
                lines.append(f"  臨床的コンテキスト: {d['annotation']}")
            if d.get("location"):
                lines.append(f"  データ所在: {d['location']}")
            # **代替候補は優先順つきで渡す**。所在は候補の配列で、
            # 「FHIR 優先・無ければ院内DB」を手順書段階で書けるようにする。
            # **キーが無ければ1行も足さない**ので、候補が1件の項目のプロンプトは変わらない。
            alternates = d.get("alternate_locations") or []
            if alternates:
                lines.append("  代替のデータ所在（この順に試す）:")
                for i, alt in enumerate(alternates, start=2):
                    lines.append(f"    第{i}候補: {alt}")
            lines.append("")
        return "\n".join(lines)

    async def generate(
        self,
        items: list[dict[str, Any]],
        model: str | None = None,
        recorder: LLMCallRecorder | None = None,
    ) -> list[GeneratedRunbookItem]:
        """項目ごとの2役割入力から手順書ステップを生成して返す

        Args:
            items: {link_id, item_text, source_category, annotation, location} の辞書リスト

        Returns:
            生成された GeneratedRunbookItem のリスト

        Raises:
            AIServiceException: Ollama呼び出し・パースエラー
        """
        if not items:
            return []

        selected_model = (model or design_llm_model()).strip()
        # **使ったモデルのダイジェストを残す**。タグだけでは実装が
        # 特定できない（同名タグの中身が差し替わっても記録は同じ文字列）。取れなくても
        # 生成は続ける——理由つきの null が残る（`model_digest_of` は例外を投げない）。
        if isinstance(recorder, DesignLlmRecorder):
            await recorder.declare_model_digest(self._client, selected_model)
        logger.info(f"手順書生成開始: {len(items)}件, model={selected_model}")

        # 多項目を1コールに詰めると num_ctx 超過で空応答になるため分割して呼ぶ。
        try:
            runbooks = await chat_json_chunked(
                self._client,
                selected_model,
                items,
                system_prompt=RUNBOOK_SYSTEM_PROMPT,
                build_message=self._build_user_message,
                # **一様なキーの省略を許さない**。既定値つきのフィールドは
                # `required` に入らず、structured output はモデルに省略を許す。
                response_format=strict_response_format(GeneratedRunbooks, *_REQUIRED_RUNBOOK_KEYS),
                parse=lambda d: GeneratedRunbooks(**d).runbooks,
                label="手順書生成",
                recorder=recorder,
                chunk_size=RUNBOOK_CHUNK_SIZE,
                # **予算は `LARGE_CONTEXT_OPTIONS`（32768）である**。
                # 既定（8192）では thinking が入らない。`think=True` に固定しているので、
                # thinking は必ず出る。thinking の長いモデルでは、8192 だと幅によって
                # 空 content になるか、content が途中で切れて parse に失敗する。
                #
                # **症状が臨床側と違う**——空 content だけでなく**壊れた JSON**でも
                # 返る。「空応答が出ないから大丈夫」と読むと見落とす。どちらも
                # `prompt_eval_count + eval_count == 8192` ちょうどで、窓を使い切った署名が
                # 一致する。
                #
                # **刻み幅では救えない。下げ先が存在しない。** 幅を削って空けた予算を
                # thinking がそのまま食いうる（幅を狭めても thinking が短くなるとは限らない）
                # ので、幅1でも閉じないことがある。しかも1コールあたりの時間が跳ね、全項目では桁違いに長くなる。したがって `RUNBOOK_CHUNK_SIZE` は 3 のまま
                # 動かさない——下げるとどの項目が同じコールに乗るかが総入れ替えになり、
                # **全項目のプロンプトが変わる**。
                #
                # **上げても壊れていなかったところは動かない**（8192 で収まっていた
                # モデルでは、32768 にしても出力は変わらないことを確かめてある）。
                options=LARGE_CONTEXT_OPTIONS,
            )
        except PartialResultException as e:
            # **完了済みチャンクを捨てない**。この経路の「未試行」は
            # **手順書を生成していない**という意味で、呼び出し側は渡した link_id と
            # 返ってきた link_id の差で数える。
            runbooks = e.partial
            logger.warning(
                f"手順書生成が途中で終わった（{e.message}）: "
                f"手順書が返ったのは{len(runbooks)}件。再実行して埋めること"
            )

        # order を 1 始まりで正規化する
        for rb in runbooks:
            for idx, step in enumerate(sorted(rb.steps, key=lambda s: s.order), start=1):
                step.order = idx

        logger.info(f"手順書生成完了: {len(runbooks)}件")
        return runbooks
