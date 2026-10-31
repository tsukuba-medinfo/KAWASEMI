"""ステップ1(役割1): 規約文書を典拠とする臨床的意味の下書き生成

臨床研究の CRF には「記入の手引き」が付随し、施設間・記入者間で解釈がぶれないように
するのが標準的な実務である。ここでは入力マニュアル・記入の手引き・実施計画書といった
**規約文書**（リソース索引の `file_role=reference`）を検索し、その該当箇所を典拠として
臨床的意味の下書きを作る。

設計方針は所在側（`resource_drafter.py`）と同じで、**LLM に典拠を捏造させない**。

- LLM が返すのは「候補の cand_id」と「記述本文」だけ
- 典拠の `unit_id` / `payload` / 出典表記は、選ばれたチャンクから**決定的に**組み立てる
  （`resource_index.derive.build_citation_source`）
- 該当箇所が無ければ `chosen_cand_id=None` を返させ、その項目は**典拠なし**として
  区別する。「規約文書を引いても意味が決まらなかった項目」を数えられる状態にしておくため

削除された `designer.py` は `HintFile` で外部文書を全文プロンプトへ貼っていた。それでは
LLM が文書に基づいて書いたのか事前学習知識で書いたのか区別できないため、この方式は
復活させない。検索でチャンクを絞り、選ばせ、アドレスを機械的に刻む。

索引が検索不可（未構築 / schema 非互換 / embedding モデル不一致）のときは、フォール
バックせずに空を返す。所在側は DB定義書 BM25 という代替経路を持つが、**典拠は索引の
アドレスがなければ成立しない**（典拠なしの記述を書くくらいなら書かない）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

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
from kawasemi.ai_input.reference_scope import in_reference_scope
from kawasemi.ai_input.resource_index.derive import (
    build_citation_source,
    is_citable_reference_unit,
)
from kawasemi.ai_input.resource_index.embedder import OllamaEmbedder
from kawasemi.ai_input.resource_index.embedding_identity import check_embedding_identity
from kawasemi.ai_input.resource_index.schema import ResourceHit
from kawasemi.ai_input.resource_index.store import get_store
from kawasemi.ai_input.schemas import (
    ClinicalAnnotationDraftCandidate,
    ClinicalAnnotationDraftItem,
    DraftState,
    LLMClinicalDrafts,
)
from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# 各項目につき提示する典拠候補の数と、1回の LLM 呼び出しに載せる項目数。
#
# 候補には**チャンク全文**（`search_text`、既定 800 文字）を載せる。抜粋（`snippet` は
# 先頭 200 文字）だけを見せると、該当規定が 201 文字目以降にあるチャンクを LLM が
# 「該当なし」と判断し、「規約文書を引いても意味が決まらなかった項目」として誤って数える。
# その件数の分子が体系的に狂うので、全文を渡す（規約文書は患者データを含まない
# 前提なので内容上の障害は無い。`docs/design_llm_provenance.md`）。
#
# 全文を載せる以上、num_ctx に収める必要がある。1コールの候補本文は
# `_CHUNK_SIZE × _TOP_K × 800` 文字で、3×2 なら 4,800 文字である。日本語を最悪 1 文字
# ＝1トークンで見積もっても約 4.8k トークン。**`_TOP_K` だけを下げて既定の 4 項目/コールを
# 保つと `_TOP_K=1` になり、LLM に選ばせる候補が無くなる**ので、項目数のほうも下げてある。
#
# **予算は `LARGE_CONTEXT_OPTIONS`（32768）である**。既定（8192）では、入力の
# 上限 6,301 文字（system 580 + schema 921 + 候補 4,800）に対して残りが約1,891 しか無く、
# `think=True` に固定しているので必ず出る thinking が、thinking の長いモデルでは
# **単独でそれを超えて** `message.content` が空になる（複数のチャンクがまとめて
# 空応答になる）。
#
# **予算が広がっても `_TOP_K` と `_CHUNK_SIZE` は動かさない。** 上げるとプロンプトの中身が
# 変わり、分割の挙動＝測定の前提が動く。**予算だけを広げれば、渡すプロンプトを1文字も
# 変えずに**その穴が閉じる。
_TOP_K = 3
_CHUNK_SIZE = 2

# 候補1件の本文の上限。indexer（`indexer/`）の `DEFAULT_CHUNK_CHARS` と同じ値を置く。
# **索引は外部成果物で、`build_info.json` にチャンク長を記録していない。** 索引側の
# 設定が変わると上の予算計算が黙って崩れ、プロンプト末尾（後ろの項目の候補）から
# 落ちる。症状は「特定の項目だけ典拠なしが増える」という形で出て原因を追いにくいので、
# 前提を祈らずにここで切り詰め、切り詰めた事実をログに残す。
#
# 本番の WebUI は索引の成果物を読むだけで indexer に依存しない（`pyproject.toml` の
# dev 依存）ので、ここでは import せずに値を置く。**両者が一致することは
# `test_clinical_drafter.py` が indexer から import して固定する。**
_MAX_CAND_CHARS = 800

# 典拠候補として提示するチャンクの最小 cosine。所在側は 0.0（足切りなし・人が選ぶ前提）
# だが、こちらは**低類似のチャンクを典拠として掴ませない**ことを優先する。無関係な
# ページを典拠に据えた記述は、典拠なしより悪い（検証者が典拠を追って初めて気づく）。
#
# `settings.resource_index_min_score` と同じ既定値だが、**意図的に独立の定数である**。
# 所在探索は人が候補から選ぶ前提で足切りを緩めてよいが、典拠は誤ったページを掴むと
# 記述の裏づけそのものが偽になる。運用設定で緩められないようにしてある。
_MIN_SCORE = 0.5

# structured output で**モデルに省略を許さないキー**。
#
# **これは測定に効く値である。** 既定値つきのフィールドは `model_json_schema()` の `required`
# に入らず、Ollama の structured output は必須でないキーの省略を許す。実測では、モデルが
# 正しい記述を書きながら `chosen_cand_id` だけを省略し、`clinical_drafter` は「典拠を選べ
# なかったら記述も採らない」方針なので**その記述を捨てていた**。この状態では
# 典拠の付与率が極端に低く出る。
#
# ここを縮めれば、索引も文書もプロンプトも1バイト変わらないまま典拠の付与率の分子が動く。
_REQUIRED_DRAFT_KEYS = ("chosen_cand_id", "annotation")

DRAFT_SYSTEM_PROMPT = """あなたはFHIR準拠EDCの入力項目に「臨床的意味」を書くアシスタントです。
臨床的意味とは、その項目に何を記入すべきかの定義（対象・起点・時間窓・優先順・条件分岐・
除外規定など）です。

各項目には次が与えられます:
- 項目名（item_text）
- 規約文書（入力マニュアル・記入の手引き・実施計画書）から検索した候補の一覧。
  各候補は cand_id（整数）と抜粋を持つ。

ルール:
- **記述は、選んだ候補の抜粋に書かれている内容だけを根拠に書くこと。**
  抜粋に無い規定を、一般的な医学知識や他施設の慣習から補ってはいけない。
- 候補の中にその項目の定義を述べたものがあれば、その cand_id を chosen_cand_id に設定し、
  annotation に日本語で記述を書く。記述は抜粋の言い換え・要約であって、新しい規定を
  加えない。
- **候補のどれもその項目の定義を述べていなければ、chosen_cand_id を null、annotation も
  null にする。** 推測で書かない。「規約文書に定義が無い」という結果そのものが必要な
  情報である。
- 典拠の文書名・ページは書かないこと（システムが候補から機械的に付与する）。
- 必ず全ての項目を drafts 配列に含めること。"""


@dataclass(frozen=True)
class ClinicalDraftResult:
    """臨床的意味のAI下書きの結果。

    **「索引が使えなかった」と「候補が無かった」を呼び出し側が区別できるようにする**
    ためのオブジェクトである。両者を件数だけで返すと、索引が検索不可のときに
    「典拠つき0件 / 典拠なし0件 / 候補なしN件」という自己矛盾した集計になり、画面は
    成功として表示してしまう（LLM は1回も呼ばれていないのにモデル名まで出る）。
    """

    items: list[ClinicalAnnotationDraftItem]
    # 典拠候補が1件も見つからなかった項目数（索引が検索不可なら全項目）。
    no_hit_count: int
    # 索引が検索可能だったか。False なら探索そのものを実行していない。
    index_searchable: bool
    # 実際に呼んだ生成モデル名。**LLM を1回も呼んでいなければ None**。
    llm_model: str | None
    # **候補はあったが下書きが1件も返らなかった項目の link_id**。全体デッドラインで
    # 打ち切られた場合と、**LLM の応答に含まれなかった**場合の両方が入る（プロンプトは全項目を
    # 含めるよう指示しているが保証はない）。**この項目は「典拠なし」ではなく「試していない」**
    # である。混ぜると「規約文書を引いても意味が決まらなかった項目」へ未試行が入る。
    unattempted_link_ids: tuple[str, ...] = ()
    # **この様式には典拠にしてよい文書が設定されていない**（`reference_scope`）。探索は
    # していないので「規約文書に定義が無い」とも違う。画面はこれを別の文言で出す
    reference_scope_empty: bool = False
    # **同じ項目に複数のエントリが返った項目**。最初のエントリを採っているが、
    # **矛盾した応答なので集計では典拠の付与率の分子からも「定義が無い」の数からも外して
    # 別掲する**。
    # 混ぜると「マニュアルに定義が無い」を過大に、典拠の付与率を過小に見せる。
    duplicated_link_ids: tuple[str, ...] = ()

    def state_of(self, item: ClinicalAnnotationDraftItem) -> str:
        """下書き1件の三状態を返す。

        **判定はここが正本である。** 以前はこの分岐が画面の JavaScript
        （`design_clinical.html` の `applyDraft`）にあり、JS のテストランナーが無いので
        レンダリング結果への文字列検査で代替していた。実測で、**2つの枝の中身を
        入れ替える変更（未試行に「典拠なし」を出し、典拠なしに未試行の文言を出す）が
        全テストを通る**ことが分かっている（`hidden` の切り替えは実行時の挙動なので、
        レンダリング結果への検査では原理的に届かない）。

        **典拠の有無を先に見る**（未試行より優先）。`unattempted` は「下書きが返らなかった
        項目」なので典拠を持たないはずだが、両方成り立つ入力が来ても
        「典拠が入っているのに未試行と表示する」ことは無い側へ倒す。
        """
        if item.citation is not None:
            return DraftState.WITH_CITATION
        if item.link_id in self.unattempted_link_ids:
            return DraftState.UNATTEMPTED
        return DraftState.NO_CITATION


def _cosine_stars(score: float) -> int:
    """cosine 類似度を適合度★（1〜3）へ変換する（所在側の `_cosine_stars` と同じ閾値）。"""
    if score >= 0.80:
        return 3
    if score >= 0.65:
        return 2
    return 1


class AIClinicalDrafter:
    """規約文書のチャンクを典拠として臨床的意味の下書きを生成する。"""

    def __init__(self, index_dir: str | None = None) -> None:
        self._index_dir = index_dir
        self._embedding_model = settings.embedding_model
        # プロバイダは設定で決まる。Ollama（既定）または vLLM。
        # `chat_json` が読むのは
        # `.message.content` だけなので、契約はそこに閉じている。
        self._client = make_design_llm_client()

    @staticmethod
    def _to_candidate(hit: ResourceHit) -> ClinicalAnnotationDraftCandidate:
        """検索ヒットを提示用の典拠候補へ変換する（出典は payload から決定的導出）。"""
        return ClinicalAnnotationDraftCandidate(
            # **提示した番号をそのまま持たせる**。再検索で振り直すと、
            # 埋め込みの僅かな差で順位が入れ替わっても誰も気づけない。
            cand_id=hit.cand_id,
            unit_id=hit.unit.unit_id,
            source=build_citation_source(hit.unit),
            snippet=hit.unit.snippet,
            payload=hit.unit.payload,
            score=hit.score,
            stars=_cosine_stars(hit.score),
        )

    def _build_user_message(self, contexts: list[tuple[str, str | None, list[ResourceHit]]]) -> str:
        """候補を **チャンク全文**（`search_text`）で提示する。

        `snippet` はチャンク先頭 200 文字の抜粋にすぎず、該当規定がそれ以降にあると
        LLM が「該当なし」と判断してしまう。出典（パス・
        ページ）は渡さない。LLM が書くのは cand_id と記述だけである。
        """
        lines = ["以下の各入力項目について、規約文書の候補から典拠を選び、記述を書いてください:\n"]
        for link_id, item_text, hits in contexts:
            lines.append(f"### linkId: {link_id}")
            lines.append(f"  項目名: {item_text or '（未設定）'}")
            lines.append("  規約文書の候補:")
            for hit in hits:
                body = (hit.unit.search_text or hit.unit.snippet or "").strip().replace("\n", " ")
                if len(body) > _MAX_CAND_CHARS:
                    logger.warning(
                        f"典拠候補の本文が上限({_MAX_CAND_CHARS}文字)を超えたので切り詰めた: "
                        f"unit_id={hit.unit.unit_id}, {len(body)}文字。"
                        "索引のチャンク長が想定と異なる可能性がある（num_ctx の予算が崩れる）"
                    )
                    body = body[:_MAX_CAND_CHARS]
                lines.append(f"    - cand_id={hit.cand_id}: {body}")
            lines.append("")
        return "\n".join(lines)

    async def draft(
        self,
        items: list[dict[str, str | None]],
        model: str | None = None,
        recorder: LLMCallRecorder | None = None,
        reference_paths: frozenset[str] | None = None,
    ) -> ClinicalDraftResult:
        """項目ごとに臨床的意味の下書きを生成して返す。

        典拠候補が見つからなかった項目も、`no_citation=True` の下書きとして
        `items` に含める（「規約文書に該当箇所が無い」ことを画面と集計に出すため）。
        **索引が検索不可のときだけは `index_searchable=False` を返し、探索を実行して
        いないことを呼び出し側が区別できるようにする。**

        Args:
            recorder: 設計ロールの LLM 来歴の受け取り口。渡すと
                プロンプト全文と生応答が記録される。既定の None では何も残らない。
            reference_paths: 典拠にしてよい文書の `path`（`reference_scope`）。None は範囲なし。
                **空集合なら探索もしない**——様式と無関係な領域の定義を写させないため
        """
        if not items:
            return ClinicalDraftResult(
                items=[], no_hit_count=0, index_searchable=True, llm_model=None
            )

        if reference_paths is not None and not reference_paths:
            logger.info(
                "この様式には典拠にしてよい文書が設定されていない。臨床的意味のAI下書きは行わない"
            )
            return ClinicalDraftResult(
                items=[],
                no_hit_count=len(items),
                index_searchable=True,
                llm_model=None,
                reference_scope_empty=True,
            )

        store = get_store(self._index_dir)
        if not store.is_searchable(self._embedding_model):
            # 所在側は BM25 へフォールバックするが、典拠は索引のアドレスが無いと
            # 成立しない。典拠のない記述を生成するより、生成しない方を選ぶ。
            logger.info(
                "リソース索引が検索不可（未構築/非互換/モデル不一致）。"
                "臨床的意味のAI下書きは行いません（典拠を刻めないため）。"
            )
            return ClinicalDraftResult(
                items=[],
                no_hit_count=len(items),
                index_searchable=False,
                llm_model=None,
            )

        embedder = OllamaEmbedder(
            model=self._embedding_model,
            base_url=settings.embedding_host,
            timeout=settings.ollama_timeout,
        )
        # **索引とクエリの embedding が同じ重みか照合する**。違えば検索を無効化する
        # （上の `is_searchable` と同じ扱い。典拠を刻めない）。取れなければ未検証として進む。
        identity = await check_embedding_identity(store, embedder)
        if not identity.compatible:
            return ClinicalDraftResult(
                items=[],
                no_hit_count=len(items),
                index_searchable=False,
                llm_model=None,
            )
        # 親の text を付けて引く（「数値(mg/dL)」だけでは何の値か分からない。`item_label`）
        queries = [(item_label(d) or "").strip() for d in items]
        qvecs = await asyncio.to_thread(embedder.embed, queries)

        contexts: list[tuple[str, str | None, list[ResourceHit]]] = []
        for d, qv, query in zip(items, qvecs, queries):
            hits = (
                store.search(
                    qv,
                    top_k=_TOP_K,
                    min_score=_MIN_SCORE,
                    # **目次・項目一覧のチャンクは典拠候補にしない**。項目名の
                    # 羅列なので字面の一致度が本文の定義文より高く、候補枠を埋める。項目に
                    # よっては上位3件がすべて目次になり、LLM に定義が1件も見えなかった
                    # （検索の取り逃がしが「定義が無い」の数を作る）。
                    predicate=lambda u: is_citable_reference_unit(u)
                    and in_reference_scope(u, reference_paths),
                )
                if query
                else []
            )
            link_id = str(d["link_id"])
            contexts.append((link_id, item_label(d), hits))  # 提示も親の text つき

        # cand_id を**全項目を通した連番**へ振り直す。
        # store は項目ごとに 0 から振るが、1回の LLM 呼び出しには複数項目が載る。
        # 番号が項目ごとに重複していると、LLM が項目 A の記述に項目 B の cand_id を
        # 付けても**項目 A の別候補として解決してしまい、記述と典拠が食い違ったまま
        # 通る**。通し番号にしておけば、取り違えは「該当なし」に落ちて検出できる。
        next_cand_id = 0
        for _link_id, _item_text, hits in contexts:
            for hit in hits:
                hit.cand_id = next_cand_id
                next_cand_id += 1

        with_hits = [c for c in contexts if c[2]]
        no_hit_count = len(contexts) - len(with_hits)

        draft_by_link: dict[str, tuple[int | None, str | None]] = {}
        selected_model: str | None = None
        # 候補が無かった項目は「試していない」ではない（探索は実行し、候補が0件だった）。
        unattempted: tuple[str, ...] = ()
        duplicated: tuple[str, ...] = ()
        if with_hits:
            # プロンプトへ入れる索引ユニットの**中身**の出所を宣言する（ガード2）。
            # `_build_user_message` が本文を載せる範囲と一致させる必要があるので、
            # 提示するのと同じ hits から取る。ここは述語で reference の本文に絞ってあるので、
            # 宣言されるのは `file:reference` だけになるはずである（ガード2 はその
            # 不変条件の検査。破れたら索引の作り方かプロンプト構築が壊れている）。
            if isinstance(recorder, DesignLlmRecorder):
                recorder.declare_content_sources(
                    hit.unit for _lid, _txt, hits in with_hits for hit in hits
                )
                # **検索に使った索引の同一性も残す**。典拠の付与率は永続化された
                # 典拠から計算するので、下書き時点の索引と測定時点の索引が別物でも値は出る。
                # 索引が使われた瞬間の記録だけが同一性を残せる。
                recorder.declare_index_identity(store)
            selected_model = (model or design_llm_model()).strip()
            # **使ったモデルのダイジェストを残す**。タグだけでは実装が
            # 特定できない（同名タグの中身が差し替わっても記録は同じ文字列）。取れなくても
            # 生成は続ける——理由つきの null が残る（`model_digest_of` は例外を投げない）。
            if isinstance(recorder, DesignLlmRecorder):
                await recorder.declare_model_digest(self._client, selected_model)
            logger.info(
                f"臨床的意味AI下書き開始: 対象{len(with_hits)}件"
                f"（典拠候補なし{no_hit_count}件）, model={selected_model}"
            )
            try:
                all_drafts = await chat_json_chunked(
                    self._client,
                    selected_model,
                    with_hits,
                    system_prompt=DRAFT_SYSTEM_PROMPT,
                    build_message=self._build_user_message,
                    # **`chosen_cand_id` を必須で要求する**。既定値つきの
                    # フィールドは `required` に入らないので、structured output は
                    # **モデルにキーの省略を許す**。実測では、モデルが正しい記述を書きながら
                    # `chosen_cand_id` だけを省略し、**その記述が捨てられていた**
                    # （典拠を選べなかったら記述も採らない方針である）。
                    response_format=strict_response_format(
                        LLMClinicalDrafts, *_REQUIRED_DRAFT_KEYS
                    ),
                    parse=lambda d: LLMClinicalDrafts(**d).drafts,
                    label="臨床的意味AI下書き",
                    chunk_size=_CHUNK_SIZE,
                    # **大きい方の num_ctx で呼ぶ**。既定（8192）では
                    # `think=True` で必ず出る thinking が予算を食い切る。
                    options=LARGE_CONTEXT_OPTIONS,
                    recorder=recorder,
                )
            except PartialResultException as e:
                # **完了済みチャンクを捨てない**。数十項目・1コール数十秒では既定の
                # 全体デッドライン（600秒）に収まらないので、ここで捨てると一連の作業の
                # 最初の一歩が何度やっても完走しない。**未着手は「典拠なし」と区別する。**
                all_drafts = e.partial
                # **件数は下の `unattempted` で数え直す**（`e.unanswered_items` はチャンク粒度の
                # 下限値なので、応答漏れがあると route のログと食い違う）。
                # **原因を1つだけ名指ししない**。`PartialResultException` は全体
                # デッドラインだけでなく、1コールのタイムアウト・空応答・JSON の崩れでも上がる。
                # 実際の原因は `e.message` にあるので添える。
                logger.warning(
                    f"臨床的意味AI下書きが途中で終わった（{e.message}）: "
                    f"下書きが返ったのは{len(all_drafts)}件。"
                    "未試行は「典拠なし」ではないので、再実行して埋めること"
                )
            # **同じ項目に2エントリ返る応答で、後勝ちで有効な下書きを捨てない**。
            # 最初を採り、**重複した項目を別に記録する**（集計が分子からも「定義が無い」の数からも
            # 外して別掲できるようにする。黙って解決しない）。
            merged = merge_by_link_id(all_drafts, value=lambda d: (d.chosen_cand_id, d.annotation))
            draft_by_link = merged.values
            duplicated = merged.duplicated_link_ids
            # **矛盾した応答を来歴へ残す**。重複した項目は「分子からも「定義が無い」の数からも
            # 外して別掲する」が、その事実を永続化しないと、永続化された典拠から数える経路
            # （集計関数・集計エンドポイント）では外せない。
            # **空でも宣言する**——「重複が無かった」と「記録していない」は違う。
            if isinstance(recorder, DesignLlmRecorder):
                recorder.declare_duplicated(duplicated)
            if duplicated:
                logger.warning(
                    f"臨床的意味AI下書き: 同じ項目に複数のエントリが返った（{len(duplicated)}件）。"
                    "最初のエントリを採ったが、**矛盾した応答なので分子に数えないこと**"
                )
            # **候補があったのに下書きが1件も返らなかった項目**を「未試行」として分ける。
            # 打ち切りで LLM へ渡らなかった場合と、渡ったが応答に含まれ
            # なかった場合（プロンプトは全項目を含めるよう指示しているが保証はない）の
            # 両方がここに入る。**どちらも「規約文書に定義が無い」ではない。**
            #
            # 順序ではなく**応答の有無**で定義する。打ち切りは末尾から起きるが、途中の
            # チャンクが飛ぶ形（応答漏れ）も同じ扱いにしたいので、並びに依存させない。
            unattempted = tuple(
                link_id for link_id, _text, _hits in with_hits if link_id not in draft_by_link
            )

        results: list[ClinicalAnnotationDraftItem] = []
        for link_id, _item_text, hits in contexts:
            cands = [self._to_candidate(hit) for hit in hits]
            chosen_id, annotation = draft_by_link.get(link_id, (None, None))

            citation: ClinicalAnnotationDraftCandidate | None = None
            if chosen_id is not None:
                idx = next((i for i, h in enumerate(hits) if h.cand_id == chosen_id), None)
                if idx is not None:
                    citation = cands.pop(idx)
                    citation.recommended = True
                    cands.insert(0, citation)

            # 典拠を選べなかったら記述も採らない。典拠のない記述は、LLM が事前学習知識で
            # 書いたものと区別できず、この機能の目的（規約文書への紐付け）を満たさない。
            text = ((annotation or "").strip() or None) if citation is not None else None
            # **記述の無い典拠も成立させない。** LLM が cand_id だけ返して annotation を
            # 空にした場合、片方だけ採ると「典拠つき」として数えられて
            # 「規約文書を引いても意味が決まらなかった項目」の集計の分子がずれ、画面は
            # 「典拠つきN件」と出すのに記述欄が空になる。候補一覧には残す。
            if text is None and citation is not None:
                citation.recommended = False  # 候補一覧に「一推し」の印を残さない
                citation = None
            results.append(
                ClinicalAnnotationDraftItem(
                    link_id=link_id,
                    annotation=text,
                    citation=citation,
                    candidates=cands,
                    no_citation=citation is None,
                )
            )

        with_citation = sum(1 for r in results if not r.no_citation)
        # **未試行を「典拠なし」に混ぜない**。ここを直さないと、同じ実行の
        # ログに「典拠なし1件」（drafter）と「典拠なし0件」（route）が並ぶ。人が読むのは
        # どちらでもありえる。
        no_citation = sum(1 for r in results if r.no_citation and r.link_id not in unattempted)
        logger.info(
            f"臨床的意味AI下書き完了: 典拠つき{with_citation}件, "
            f"典拠なし{no_citation}件（うち候補なし{no_hit_count}件）, "
            f"未試行{len(unattempted)}件"
        )
        return ClinicalDraftResult(
            items=results,
            no_hit_count=no_hit_count,
            index_searchable=True,
            llm_model=selected_model,
            unattempted_link_ids=unattempted,
            duplicated_link_ids=duplicated,
        )
