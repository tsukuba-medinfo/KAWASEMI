"""ステップ1(役割2): データ所在の「AIによる案の作成」生成層

各Questionnaire項目について、項目テキスト＋役割1アノテーションをクエリに DB定義書
コーパス（db_definition.py のBM25）からカラム候補を絞り込み、Ollamaに「どの候補が
最も妥当か（cand_id）」と source_category を選定させる。

設計方針:
- LLMは候補の cand_id を選ぶだけ。location 文字列は ColumnHit.build_location() で
  決定的に組み立てる（存在しないカラム名の捏造を構造的に防止）。
- 候補ゼロの項目はLLMに渡さず未入力スキップ。
- source_category は (B)要件によりLLMが5カテゴリから判断（候補ありは通常 local_db）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kawasemi.ai_input.db_definition import ColumnHit, get_index
from kawasemi.ai_input.design_provenance import DesignLlmRecorder
from kawasemi.ai_input.llm_json import (
    LARGE_CONTEXT_OPTIONS,
    LLMCallRecorder,
    PartialResultException,
    chat_json_chunked,
    merge_by_link_id,
    strict_response_format,
)
from kawasemi.ai_input.llm_provider import design_llm_model, make_design_llm_client
from kawasemi.ai_input.location_hint import item_label
from kawasemi.ai_input.models import SourceCategory
from kawasemi.ai_input.schemas import (
    LLMLocationChoices,
    SystemLocationDraftCandidate,
    SystemLocationDraftItem,
)
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# BM25で各項目につき提示する候補数（top-k選択UIの母数）
_TOP_K = 8

# structured output で**モデルに省略を許さないキー**。
#
# **所在候補は所在の確からしさの段階（rubric）の入力である。** 既定値つきのフィールドは
# `model_json_schema()` の `required` に入らず、structured output は省略を許す。
# `chosen_cand_id` が省略されると `recommended` が付かず、画面の初期選択が「未選択」になる
# （`design_system.html`）。人が選ばなければ所在は空のまま＝段階分布が `ABSENT`／判定不能へ
# 倒れる。典拠側で実測された欠陥（典拠の付与率が極端に低く出た）と同じ形である。
#
# **`source_category` は入れない。** LLM の返した `source_category` は
# `choice_by_link = {c.link_id: c.chosen_cand_id ...}` で捨てられ、候補の種別は
# `SourceCategory.LOCAL_DB` を直に置いている（`resource_drafter` は `derive_source_category`）。
# **読まれない値を必須にしても測定は動かない**——効かない要求を層として数えない。
_REQUIRED_CHOICE_KEYS = ("chosen_cand_id",)


def _bm25_stars(score: float, top_score: float) -> int:
    """BM25スコアを適合度★（1〜3）に変換する。

    BM25は非正規（語数・コーパス依存）で絶対値に意味が薄いため、その項目内の最高
    スコアに対する相対比で段階化する。索引経路（cosine 絶対閾値）とは別尺度。
    """
    if top_score <= 0:
        return 1
    ratio = score / top_score
    if ratio >= 0.66:
        return 3
    if ratio >= 0.33:
        return 2
    return 1


DRAFT_SYSTEM_PROMPT = """あなたはFHIR準拠のEDC（電子データ収集）システムのAIアシスタントです。
各入力項目について、施設のDB定義書から抽出した「カラム候補」のうち、その項目の値が
格納されていると最も考えられる候補を1つ選んでください。

各項目には次が与えられます:
- 項目名（item_text）と、臨床的コンテキスト（annotation, 任意）
- 候補カラムの一覧。各候補は cand_id（整数）と、所属DB（db_group）/ 表（db_qualified,
  table_logical）/ カラム（physical_name, logical_name）/ 型 / 主キー / メモ（notes）を持つ。

ルール:
- 候補の中に妥当なものがあれば、その cand_id を chosen_cand_id に設定する。
- 候補の物理名・論理名・メモと項目名/臨床的コンテキストの意味を照らし、最も合致するものを選ぶ。
  語彙が完全一致しなくても意味的に一致すれば選んでよい（例「初診日」↔「受付日」）。
- 妥当な候補が無ければ chosen_cand_id を null にする（推測でカラムを選ばない）。
- source_category は次から1つ選ぶ:
  - local_db: 候補（=施設の院内DB）を選んだ場合。原則これを使う。
  - fhir: 候補は無いが、FHIRサーバから標準的に取得できる項目（患者基本情報・観察値など）。
  - manual: 候補が無く、人手入力が妥当な場合。
  - external_db / llm: 明確な根拠がある場合のみ。
- 候補に無いカラム名・表名を新たに作り出さないこと。選ぶのは cand_id のみ。
- 必ず全ての項目を choices 配列に含めること。"""


@dataclass
class LocationDraftResult:
    """データ所在のAI案の結果。**BM25 経路と索引経路の共通の戻り値**である。

    以前はタプル `(drafts, no_hit_count)` を返していたので、**「一推しが返らなかった項目」を
    呼び出し側へ渡せなかった**。失敗したチャンクを飛ばして続けるようになると、
    1回の実行の中に「候補を見せて一推しが返った項目」「見せたが返らなかった項目」
    「候補が0件だった項目」が同時に存在しうる。**混ぜると所在の確からしさの段階（rubric）の
    入力が汚染される**（未試行が「妥当な候補なし」として数えられる）。

    臨床側の `ClinicalDraftResult` と**同じキー名**で持つ（画面と API を同じ形にする）。
    """

    items: list[SystemLocationDraftItem]
    # 典拠候補が1件も見つからなかった項目数（探索は実行している。「未試行」ではない）。
    no_hit_count: int
    # 実際に呼んだ生成モデル名。**LLM を1回も呼んでいなければ None**（臨床側と同じ規則。
    # 候補が0件で LLM を呼んでいないのにモデル名を出すと、画面は生成したように見える）。
    llm_model: str | None
    # **候補はあったが一推し（`chosen_cand_id` のエントリ）が返らなかった項目の link_id**
    # 打ち切り・チャンクの失敗・応答漏れの3つが入る。**「妥当な候補なし」
    # （`chosen_cand_id=null` が返った項目）とは別**である。前者は押し直せば埋まる。
    unattempted_link_ids: tuple[str, ...] = ()
    # **同じ項目に複数のエントリが返った項目**（臨床側と同じ扱い）。最初の
    # エントリを採っているが、**矛盾した応答なので集計では別掲する**。
    duplicated_link_ids: tuple[str, ...] = ()


@dataclass
class _ItemContext:
    """1項目の入力コンテキストと、その項目に対するBM25候補"""

    link_id: str
    item_text: str | None
    annotation: str | None
    hits: list[ColumnHit]


class AILocationDrafter:
    """ステップ1(役割2): データ所在のAI案を生成するクラス"""

    def __init__(self, corpus_dir: str | None = None) -> None:
        self._corpus_dir = corpus_dir
        # プロバイダは設定で決まる。Ollama（既定）または vLLM。
        # `chat_json` が読むのは
        # `.message.content` だけなので、契約はそこに閉じている。
        self._client = make_design_llm_client()

    def _retrieve(self, items: list[dict[str, Any]]) -> list[_ItemContext]:
        """各項目についてBM25で候補カラムを絞り込む。候補ゼロの項目は除外しない
        （呼び出し側で件数を集計するため hits=[] のまま保持）。"""
        index = get_index(self._corpus_dir)
        contexts: list[_ItemContext] = []
        for d in items:
            item_text = item_label(d)  # 親の text つき（クエリと提示だけ。保存値は呼び出し側）
            annotation = d.get("annotation")
            query = " ".join(p for p in (item_text, annotation) if p)
            hits = index.search(query, top_k=_TOP_K) if query else []
            contexts.append(
                _ItemContext(
                    link_id=d["link_id"],
                    item_text=item_text,
                    annotation=annotation,
                    hits=hits,
                )
            )
        return contexts

    def _build_user_message(self, contexts: list[_ItemContext]) -> str:
        lines = [
            "以下の各入力項目について、候補カラムから最も妥当なものの cand_id を選んでください:\n"
        ]
        for ctx in contexts:
            lines.append(f"### linkId: {ctx.link_id}")
            lines.append(f"  項目名: {ctx.item_text or '（未設定）'}")
            if ctx.annotation:
                lines.append(f"  臨床的コンテキスト: {ctx.annotation}")
            lines.append("  候補カラム:")
            for hit in ctx.hits:
                cand = hit.to_candidate()
                lines.append(
                    f"    - cand_id={cand['cand_id']}: "
                    f"DB={cand['db_group']} 表={cand['db_qualified']}"
                    f"（{cand['table_logical']}）"
                    f" カラム={cand['physical_name']}（{cand['logical_name']}）"
                    f" 型={cand['type']} PK={cand['pk']}"
                    + (f" メモ={cand['notes']}" if cand["notes"] else "")
                )
            lines.append("")
        return "\n".join(lines)

    async def draft(
        self,
        items: list[dict[str, Any]],
        model: str | None = None,
        recorder: LLMCallRecorder | None = None,
    ) -> LocationDraftResult:
        """項目ごとにデータ所在のAI案を生成して返す。

        Args:
            items: {link_id, item_text, annotation} の辞書リスト

        Returns:
            :class:`LocationDraftResult`（未試行と重複を別に持つ）

        Raises:
            AIServiceException: Ollama呼び出し・パースエラー（**部分結果すら無いとき**）
        """
        if not items:
            return LocationDraftResult(items=[], no_hit_count=0, llm_model=None)

        contexts = self._retrieve(items)
        with_hits = [c for c in contexts if c.hits]
        no_hit_count = len(contexts) - len(with_hits)

        # LLM一推し（cand_id）を取得。候補がある項目だけ提示する。
        choice_by_link: dict[str, int | None] = {}
        selected_model: str | None = None
        unattempted: tuple[str, ...] = ()
        duplicated: tuple[str, ...] = ()
        if with_hits:
            selected_model = (model or design_llm_model()).strip()
            # **使ったモデルのダイジェストを残す**。タグだけでは実装が
            # 特定できない（同名タグの中身が差し替わっても記録は同じ文字列）。取れなくても
            # 生成は続ける——理由つきの null が残る（`model_digest_of` は例外を投げない）。
            if isinstance(recorder, DesignLlmRecorder):
                await recorder.declare_model_digest(self._client, selected_model)
            logger.info(
                f"データ所在AI案生成開始: 対象{len(with_hits)}件"
                f"（候補なし{no_hit_count}件）, model={selected_model}"
            )
            # 候補リストを含むためプロンプトが大きい。コンテキスト超過による空応答を避ける
            # ため項目を分割してOllamaを呼び結果を統合する（分割は chat_json_chunked が担う）。
            try:
                all_choices = await chat_json_chunked(
                    self._client,
                    selected_model,
                    with_hits,
                    system_prompt=DRAFT_SYSTEM_PROMPT,
                    build_message=self._build_user_message,
                    # **`chosen_cand_id` を必須で要求する**。既定値つきの
                    # フィールドは `required` に入らず、structured output はモデルにキーの省略を
                    # 許す。所在候補は**所在の確からしさの段階（rubric）の入力**なので、省略が黙って「該当なし」になると
                    # rubric 段階の分布が動く。
                    response_format=strict_response_format(
                        LLMLocationChoices, *_REQUIRED_CHOICE_KEYS
                    ),
                    parse=lambda d: LLMLocationChoices(**d).choices,
                    label="データ所在AI案",
                    # 候補8件×4項目の提示は 8192 に収まらない（1万2千字前後になる）。
                    options=LARGE_CONTEXT_OPTIONS,
                    recorder=recorder,
                )
            except PartialResultException as e:
                # **完了済みチャンクを捨てない**。所在候補は**所在の確からしさの
                # 段階（rubric）の入力**なので、対象項目のうち一部しか返らなかった実行を
                # 全部捨てると、一連の作業が何度やっても完走しない。
                # **原因を1つだけ名指ししない**。打ち切り・チャンクの失敗・
                # 応答漏れのどれかなので、実際の原因は `e.message` にある。
                all_choices = e.partial
                logger.warning(
                    f"データ所在AI案が途中で終わった（{e.message}）: "
                    f"一推しが返ったのは{len(all_choices)}件。"
                    "未試行は「妥当な候補なし」ではないので、再実行して埋めること"
                )
            # **後勝ちで有効な選択を捨てない**（臨床側と同じ規則を使う）。
            merged = merge_by_link_id(all_choices, value=lambda c: c.chosen_cand_id)
            choice_by_link = merged.values
            duplicated = merged.duplicated_link_ids
            if duplicated:
                logger.warning(
                    f"データ所在AI案: 同じ項目に複数のエントリが返った"
                    f"（{len(duplicated)}件）。最初のエントリを採ったが、"
                    "**矛盾した応答なので所在の確からしさの集計から外すこと**"
                )
            # **候補があったのに一推しのエントリが返らなかった項目**を「未試行」として分ける。
            # `chosen_cand_id=null` が返った項目は**エントリがある**ので
            # ここに入らない（「妥当な候補なし」＝結果であって未試行ではない）。
            # 順序ではなく**応答の有無**で定義する（途中のチャンクが飛ぶ形も同じ扱いにする）。
            unattempted = tuple(c.link_id for c in with_hits if c.link_id not in choice_by_link)

        drafts: list[SystemLocationDraftItem] = []
        for ctx in contexts:
            top_score = max((h.score for h in ctx.hits), default=0.0)
            # BM25候補はDB定義書のカラム＝院内DB。location は決定的に組み立てる。
            cands = [
                SystemLocationDraftCandidate(
                    source_category=SourceCategory.LOCAL_DB,
                    location=hit.build_location(),
                    score=hit.score,
                    stars=_bm25_stars(hit.score, top_score),
                )
                for hit in ctx.hits
            ]
            # LLM一推しを先頭へ繰り上げ＋recommended。一推し無し（null）なら据え置き。
            chosen = choice_by_link.get(ctx.link_id)
            if chosen is not None:
                rec_idx = next((i for i, h in enumerate(ctx.hits) if h.cand_id == chosen), None)
                if rec_idx is not None:
                    rec = cands.pop(rec_idx)
                    rec.recommended = True
                    cands.insert(0, rec)
            drafts.append(SystemLocationDraftItem(link_id=ctx.link_id, candidates=cands))

        filled = sum(1 for d in drafts if d.candidates)
        logger.info(
            f"データ所在AI案生成完了(BM25): {filled}件に候補, 候補なし{no_hit_count}件, "
            f"未試行{len(unattempted)}件"
        )
        return LocationDraftResult(
            items=drafts,
            no_hit_count=no_hit_count,
            llm_model=selected_model,
            unattempted_link_ids=unattempted,
            duplicated_link_ids=duplicated,
        )
