"""ステップ1(役割2): データ所在の「AIによる案の作成」生成層（embedding検索版）

リソース索引（ResourceIndexStore）に対し、項目テキスト＋役割1アノテーションを
クエリベクトルとして cosine 検索し、kind横断の候補（db/file/fhir）を Ollama に提示。
LLM は候補の cand_id を選ぶだけで、location文字列・source_category は payload から
決定的に導出する（カラム名等の捏造を構造的に防止）。

索引が未構築／schema非互換／embeddingモデル不一致のときは、従来の DB定義書 BM25
（AILocationDrafter）へフォールバックする（施設が索引を作るまで縮退して動き続ける）。

設計方針:
- LLMは cand_id 選択のみ。location/source_category は derive で決定的導出。
- 候補ゼロの項目はLLMに渡さず未入力スキップ（件数のみ集計）。

検索の**前に**、院内での呼ばれ方と想定される型を LLM に書かせる
（`location_hint`）。生成した語はクエリ文へ足して埋め込み、型は候補の並べ替えの加点に
使う。**生成テキストはクエリベクトルにしかならず**、所在文字列は従来どおり
`build_location` が payload から組むので、上の設計方針は変わらない。
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

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
from kawasemi.ai_input.location_drafter import AILocationDrafter, LocationDraftResult
from kawasemi.ai_input.location_hint import (
    LocationHint,
    generate_hints,
    item_label,
    type_matches,
)
from kawasemi.ai_input.location_rules import load_location_rules
from kawasemi.ai_input.resource_index.connections import get_registry
from kawasemi.ai_input.resource_index.derive import (
    build_location,
    derive_source_category,
    is_reference_unit,
)
from kawasemi.ai_input.resource_index.embedder import OllamaEmbedder
from kawasemi.ai_input.resource_index.embedding_identity import check_embedding_identity
from kawasemi.ai_input.resource_index.schema import Kind, ResourceHit, ResourceUnit
from kawasemi.ai_input.resource_index.store import ResourceIndexStore, get_store
from kawasemi.ai_input.schemas import (
    LLMLocationChoices,
    SystemLocationDraftCandidate,
    SystemLocationDraftItem,
)
from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# 各項目につき検索で提示する候補数（top-k選択UIで上位3件＋「もっと見る」展開の母数）
_TOP_K = 8

# 型ヒントで並べ替える前に索引から取る候補数。
#
# **加点は提示枠の中だけでは効かない。** `_TOP_K`(8) しか取らずに並べ替えても、8枠が
# 名前の似た別物で埋まっていれば正しい列はそもそも入ってこない（例: 同じ語を名前に持つ
# 採番管理の小さな表がコサイン上位に来て、本命の検査結果の表が入らない）。プールを
# 広げて取り、加点してから `_TOP_K` へ絞る。
#
# **プロンプトへ載る件数は変わらない**（絞ったあとが提示なので `_TOP_K` のまま）。
# 増えるのは索引検索の取り出し件数だけで、LLM の入力長には効かない。
#
# **測定に効く**（提示される候補の母集合が変わる）。
_BOOST_POOL = 40

# 第1段（表）で残す表の数。
#
# **索引は列ごとに1本のベクトルしか持たず、1本に「どの表か」と「その表のどの列か」の
# 両方を負わせている。** 列数の少ない採番管理の表がコサイン上位に来て、本命の列の多い
# 表が `_TOP_K=8` の枠に入らないことがある。`_column_search_text` は表名と列名を連結
# するので、**列数が少なく短い表ほど語の濃度が高くなる**。第1段で表を絞れば、どちらの
# 仕事も専用の表現が担う。
#
# **値の根拠**: 代理クエリで表の重心を並べると、採番管理の表は大きく順位を落とす一方、
# 本命の表も上位30位の外に出ることがあった。30 では**本命の表を第1段で落とす**。
# 100 なら表の上位だけを残しつつ、走査対象の列は大きく減る。
# **第1段は長い尾を落とすためのもので、当てにいくものではない。** 正しい表を落とす代償
# （その項目の列が到達不能になる）のほうが、絞りが甘い代償より高い。
#
# **暫定値であり、正解データ（人手で確定した所在）が揃ったら調整する。**
#
# **測定に効く**（第2段の母集合が変わる）。
_TABLE_TOP_N = 100

# RRF（Reciprocal Rank Fusion）の平滑化定数。順位 r の寄与は `1/(k+r)`。
# 60 は RRF の慣例値で、indexer（`indexer/`）の `lexical` 側でも同じ値を使っている。
#
# **測定に効く**（併合後の並びが変わる）。
#
# 診療記録の本文への受け皿として上位 `_TOP_K` 件の**後ろへ**足す単位は、
# 施設の文書体系で決まるので `site/location_rules.yaml` の `fallback_unit_ids` に置く
# （`kawasemi/ai_input/location_rules.py`）。無ければ受け皿を足さない。
#
# **提示枠の8件は1件も減らない。** 押し込むのではなく append するので、実在の候補が
# 押し出されることは構造上起きない。
#
# 複数の文書種別を置けるのは、**画面に非LLMの検索経路が無い**ためである（規約文書の
# 典拠ピッカーに相当するものが所在側に無い）。経過記録だけを出すと、正しい所在が
# 手術記録や初診時記録だと人が判断した項目は結局手動入力になり、`unit_id` が null に
# なって `derive_op_args` が動かない。**選ぶのは人**なので、件数が増えても判断は機械に
# 寄らない。
#
# **測定に効く**（人が選べる所在の集合が変わる）ので、規則のファイルも同じく扱う。

_RRF_K = 60

# 1つの表が提示枠に占められる上限は**置かない**（以前は置いていたが外した）。
#
# 置いた根拠は「列ベクトルの上位8件は**すべて同じ表**の列になる」だったが、これは
# 列の重心を代理クエリにしたことによる**自己一致**で、項目から作るクエリには当てはまらない
# 前提だった。根拠が無いまま上限を置くと、**提示の顔ぶれを変える**作用だけが残る。
#
# **押し出されたのが正しい列だったかは分からない。** 効果不明の操作を人の確定作業に
# 掛けると、確定データそのものが影響を受ける。だから確定の前に外す。
#
# これは確定データで定数を調整したのではない。**入れた根拠が成り立たなかった**ので
# 戻しただけであり、循環しない。

# 同じ label が索引に何度も出る候補（定型列）を下げる係数。
# 並べ替えのキーから `_DF_PENALTY * log(その label の出現回数)` を引く。
#
# **狙いは「コード」のような汎用語ではなく、コピーされた定型列である。** 施設コードや
# 診療科コードのように全表へ同じ物理名・論理名で複製される列は、索引に数百回出て、
# 「〜コード」を問う項目の提示枠を占めてしまう。一方、固有の列の label の多くは1回しか
# 出ないので、出現回数はそのまま「その名前がどれだけ固有か」になる。
#
# **`hit.score` は書き換えない**（`_TYPE_BOOST` と同じ理由。★は素の cosine のままにする）。
#
# **値は実測から決めた。** 0 では定型列が1位に来る。強すぎると、患者IDのように正解
# そのものが定型列である項目で正解を沈める（`0.01` で実際に沈んだ）。0.002 なら数百回
# 出る列で `log(n)*0.002` は約 0.012 で、プール幅の数割にあたる。
#
# **測定に効く**（提示順が変わる）。
_DF_PENALTY = 0.002

# 型が一致した候補へ足すスコア。
#
# **`hit.score` は書き換えない。** 素の cosine は ★（`_cosine_stars`）の入力であり、
# 画面にも出る「適合度」なので、加点で動かすと表示の意味が変わる。加点は並べ替えの
# キーにだけ使い、候補が持つ score は索引が返した値のまま残す。
#
# **値は実測から決めた**（対象項目・`top_k=40`・`reference` 除外・
# クエリは `item_text + 人が書いた臨床的意味`）。
#
# **プール40件のスコアは狭い幅（0.05 を大きく下回る）に収まり、隣接順位の差はさらに
# 一桁小さい。** 最初に置いた 0.05 はプール幅を超えるので、型が一致した候補が一致しなかった
# 候補を全部飛び越える（ほぼ全項目で40位から8枠へ届く）。それは加点ではなく**プール内のハードフィルタ**であり、「外したときに
# 正しい列が到達不能になるのを避けるため加点にする」という方針と矛盾していた。
#
# 0.005 はプール幅の数分の1、隣接順位の差の数ランク相当である。
# **同程度に似ている候補の中で型が合うほうを上へ出すが、プール全体は並べ替えない。**
#
# **測定に効く**（提示順が変わる）。
_TYPE_BOOST = 0.005

# プロンプトへ載せる候補本文（snippet）の上限。
#
# **索引側が切っていることに依存しない。** 実装上は `kawasemi_indexer` の file_indexer が
# `snippet=piece[:200]` で切っているが、それは**索引側の実装の都合**であって
# こちら側の保証ではない。索引の作り方が変われば、`_TOP_K`(8) × 刻み幅(4) = 1コール32候補の
# 本文がそのままプロンプトへ入る（snippet が 800 文字なら3万字を超える）。
#
# 臨床的意味の下書き（`clinical_drafter._MAX_CAND_CHARS = 800`）と同じく、**入力側の上限は
# 索引のチャンク長が変わっても動かない**形で持つ。現行の索引のチャンク長（200）に合わせるので、
# いまの索引に対しては切り詰めは発生しない（＝この変更でプロンプトは1文字も変わらない）。
#
# **測定に効く**（提示する候補本文の量が所在の選ばれ方を決める）。
_MAX_SNIPPET_CHARS = 200

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
# `derive_source_category`（索引の payload から決定的に導出）が決める。
# **読まれない値を必須にしても測定は動かない**。
_REQUIRED_CHOICE_KEYS = ("chosen_cand_id",)


def _cosine_stars(score: float) -> int:
    """cosine類似度（0〜1）を適合度★（1〜3）に変換する。

    索引経路は L2 正規化済みベクトルの内積なので絶対値に意味がある。閾値は実装時の
    暫定値（運用で調整可）。BM25経路（location_drafter）とは尺度が異なるため別関数。
    """
    if score >= 0.80:
        return 3
    if score >= 0.65:
        return 2
    return 1


DRAFT_SYSTEM_PROMPT = """あなたはFHIR準拠のEDC（電子データ収集）システムのAIアシスタントです。
各入力項目について、施設のリソース索引から検索した「候補」のうち、その項目の値が
格納されていると最も考えられる候補を1つ選んでください。

各項目には次が与えられます:
- 項目名（item_text）と、臨床的コンテキスト（annotation, 任意）
- 候補の一覧。各候補は cand_id（整数）/ 種別（DB・ファイル・FHIR）/ 名称 / 抜粋 を持つ。

ルール:
- 候補の中に妥当なものがあれば、その cand_id を chosen_cand_id に設定する。
- 名称・抜粋と項目名/臨床的コンテキストの意味を照らし、最も合致するものを選ぶ。
  語彙が完全一致しなくても意味的に一致すれば選んでよい（例「初診日」↔「受付日」）。
- 妥当な候補が無ければ chosen_cand_id を null にする（推測で候補を選ばない）。
- source_category は補助情報であり、候補を選んだ場合はシステム側で種別から決定する。
  候補が無い場合のみ fhir / manual / llm から妥当なものを選んでよい。
- 必ず全ての項目を choices 配列に含めること。"""


@dataclass
class _ItemContext:
    """1項目の入力コンテキストと、その項目に対する検索候補"""

    link_id: str
    item_text: str | None
    annotation: str | None
    hits: list[ResourceHit]
    label: str | None = None  # 提示用の項目名（親の text つき。`item_label`）


def _query_text(item: dict[str, Any], hint: LocationHint) -> str:
    """索引を引くクエリ文を組み立てる。

    項目名＋臨床的意味の**後ろに**院内での呼ばれ方を足す。前に置かないのは、埋め込みが
    先頭に寄る性質を持つ実装があるためで、項目そのものの意味を薄めないためである。
    ヒントが無ければ従来と同じ文字列になる（施策抜きで縮退する）。
    """
    base = " ".join(p for p in (item_label(item), item.get("annotation")) if p)
    # **項目そのものに文字が無ければ拡張しない。** 検索するかどうかの門は
    # 「項目にテキストがあるか」であって（この版より前と同じ）、ヒントで門を開けない。
    # 開けると、項目名も臨床的意味も無い項目に、モデルが出した語だけで候補が付く。
    if not base:
        return ""
    return " ".join([base, *hint.terms])


def _short_query_text(item: dict[str, Any], hint: LocationHint) -> str:
    """名前の一致を拾うための**短いクエリ**を組み立てる。

    臨床的意味（数百〜千字）を外し、項目名とヒント語だけにする。

    **長いクエリでは短い索引テキストが埋没する。** たとえば病理診断の項目に対する
    病理レポートの文書種別は、項目名＋臨床的意味＋語（数百字）だと上位から大きく
    沈み、項目名＋語や語だけなら最上位近くに来る。索引側の `search_text` は数十字なので、
    千字のクエリとは長さが釣り合わない。

    **語だけにしないのは、ヒントが無いときに空になるからである。** 施策抜きで縮退する
    という他の施策と同じ扱いにして、ヒントが無ければ項目名だけの短いクエリになる。

    **長いクエリを置き換えるのではない。** 項目によっては長いクエリの上位が妥当な列で、
    短いクエリにすると項目名の一部の字面だけが合う無関係な列へ変わり悪化する。長いほうは
    項目の意味を、短いほうは名前の一致を捉えるので、併合する。
    """
    name = item_label(item) or ""
    if not name:
        return ""
    return " ".join([name, *hint.terms])


def _rrf_merge(ranked: list[list[ResourceHit]], top_k: int, k: int = _RRF_K) -> list[ResourceHit]:
    """複数の順位づけを RRF（Reciprocal Rank Fusion）で併合する。

    順位 r（0始まり）の寄与は `1/(k + r + 1)`。**スコアの値ではなく順位で混ぜる**ので、
    クエリの長さが違って cosine の絶対値が揃わなくても比較できる。一般に短いクエリの
    上位ほど cosine が高く出るので、そのまま足すと短いほうが常に勝つ。

    `k=60` は RRF の慣例値で、indexer（`indexer/`）の `lexical` 側でも同じ値を使っている。
    **枠を何件割り当てるかという定数を置かずに済む**のが RRF を選んだ理由である。

    残す `ResourceHit.score` は**そのユニットが得た cosine のうち高いほう**である。
    ★（`_cosine_stars`）はこの値を見るので、画面の★は「2つのクエリのうち近いほう」になる。
    """
    fused: dict[str, float] = {}
    best: dict[str, ResourceHit] = {}
    for hits in ranked:
        for r, h in enumerate(hits):
            uid = h.unit.unit_id
            fused[uid] = fused.get(uid, 0.0) + 1.0 / (k + r + 1)
            if uid not in best or h.score > best[uid].score:
                best[uid] = h
    order = sorted(fused, key=lambda u: (-fused[u], -best[u].score, u))
    return [best[u] for u in order[:top_k]]


def _in_tables(unit: Any, tables: set[tuple[str, str]]) -> bool:
    """第1段で残した表に属するか。

    **表を持たない種別（`file` / `fhir` / `master`）は落とさない。** 表の2段階検索は
    DB のカラムの問題であって、ファイルや FHIR の所在候補まで巻き込む理由がない。
    """
    key = ResourceIndexStore.table_key(unit)
    return key is None or key in tables


def retrieve_hits(
    store: ResourceIndexStore,
    hint: LocationHint,
    qvec: Any,
    svec: Any | None,
    *,
    top_k: int = _TOP_K,
) -> list[ResourceHit]:
    """1項目ぶんの候補を索引から取る（**本番の所在探索の本体**）。

    `AIResourceDrafter.draft` と `kawasemi index search` の**両方がここを呼ぶ**。
    以前は CLI 側に別の検索が書かれており、規約文書の除外も
    クエリ拡張・型加点も入っていなかったため、「なぜこの列が提案された／
    されなかった」を説明する道具が本番と別の順位を出していた。**同じ関数を通すことでしか
    一致は保てない**——写した実装は必ずずれる（その間にも本番だけがさらに動いた）。

    含むもの: 表の第1段絞り込み、規約文書の除外、空の列の除外、
    長短2クエリの RRF 併合、型ヒントによる加点と定型列の抑制。

    `svec` が None なら短いクエリを引かない（項目名が無い場合の縮退）。
    """

    def _acquirable(u: ResourceUnit) -> bool:
        return store.column_fill.is_acquirable(u)

    def _pool_for(vec: Any) -> list[ResourceHit]:
        tables = set(store.search_tables(vec, _TABLE_TOP_N))
        pool = store.search(
            vec,
            top_k=_BOOST_POOL,
            min_score=0.0,
            predicate=lambda u: not is_reference_unit(u)
            and _acquirable(u)
            and (not tables or _in_tables(u, tables)),
        )
        # **第1段が正しい表を落としたときに列を到達不能にしない**。
        # 絞り込みの結果が提示枠に届かなければ、全体検索から足りない分を補う。
        # 加点をハードフィルタにしなかったのと同じ理由で、「LLM が
        # 提案しなかった所在」を増やさないためである。
        if len(pool) < top_k and tables:
            seen = {h.unit.unit_id for h in pool}
            fallback = store.search(
                vec,
                top_k=_BOOST_POOL,
                min_score=0.0,
                predicate=lambda u: not is_reference_unit(u) and _acquirable(u),
            )
            pool = pool + [h for h in fallback if h.unit.unit_id not in seen]
        return pool

    # 長いクエリと短いクエリを**同じ経路で**引き、順位で併合する。
    # 短いクエリが空になるのは項目名が無いときで、そのときは長いほうだけで縮退する。
    ranked = [_pool_for(qvec)]
    if svec is not None:
        ranked.append(_pool_for(svec))
    pool = _rrf_merge(ranked, _BOOST_POOL) if len(ranked) > 1 else ranked[0]
    return _rerank_by_type(pool, hint, top_k, store)


def _rerank_by_type(
    hits: list[ResourceHit],
    hint: LocationHint,
    top_k: int,
    store: ResourceIndexStore | None = None,
) -> list[ResourceHit]:
    """型で加点し、定型列を下げて並べ替え、上位 ``top_k`` へ絞る。

    並べ替えのキーは次のとおり。**`hit.score` そのものは書き換えない**（★は素の cosine）。

        key = score + (型が一致すれば _TYPE_BOOST) - _DF_PENALTY * log(label の出現回数)

    `cand_id` は**振り直す**。`ResourceHit.cand_id` は「LLM へ提示するときの 0 始まり
    連番」と定義されており、プールから絞ったあとの並びで連番になっていなければ
    ならない（提示とプロンプトと選択の突き合わせが同じ番号で回る）。

    **どちらの施策も無ければ並びは索引の順のまま変わらない**（施策抜きで縮退する）。
    型ヒントが無ければ加点は効かず、`store` を渡さなければ定型列も下げない。
    """
    if not hint.type_keys and store is None:
        return [h.model_copy(update={"cand_id": i}) for i, h in enumerate(hits[:top_k])]

    def key(hit: ResourceHit) -> float:
        k = hit.score
        if hint.type_keys and type_matches(hint, hit.unit.payload.get("type_raw")):
            k += _TYPE_BOOST
        if store is not None:
            k -= _DF_PENALTY * math.log(store.label_count(hit.unit.label))
        return -k

    # `sorted` は安定なので、キーが同じ候補どうしは索引の順（score 降順）を保つ。
    return [
        h.model_copy(update={"cand_id": i}) for i, h in enumerate(sorted(hits, key=key)[:top_k])
    ]


class AIResourceDrafter:
    """ステップ1(役割2): リソース索引のembedding検索でデータ所在AI案を生成する。"""

    def __init__(self, index_dir: str | None = None, corpus_dir: str | None = None) -> None:
        self._index_dir = index_dir
        self._corpus_dir = corpus_dir
        self._embedding_model = settings.embedding_model
        # プロバイダは設定で決まる。Ollama（既定）または vLLM。
        # `chat_json` が読むのは
        # `.message.content` だけなので、契約はそこに閉じている。
        self._client = make_design_llm_client()

    def _build_user_message(self, contexts: list[_ItemContext]) -> str:
        lines = ["以下の各入力項目について、候補から最も妥当なものの cand_id を選んでください:\n"]
        for ctx in contexts:
            lines.append(f"### linkId: {ctx.link_id}")
            lines.append(f"  項目名: {ctx.label or ctx.item_text or '（未設定）'}")
            if ctx.annotation:
                lines.append(f"  臨床的コンテキスト: {ctx.annotation}")
            lines.append("  候補:")
            for hit in ctx.hits:
                kind_label = Kind.LABELS.get(hit.unit.kind, hit.unit.kind)
                snippet = hit.unit.snippet[:_MAX_SNIPPET_CHARS]
                lines.append(
                    f"    - cand_id={hit.cand_id}: [{kind_label}] {hit.unit.label}"
                    + (f" — {snippet}" if snippet else "")
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

        索引が検索可能でなければ DB定義書 BM25（AILocationDrafter）へフォールバック。

        Returns:
            :class:`LocationDraftResult`（未試行と重複を別に持つ）
        """
        if not items:
            return LocationDraftResult(items=[], no_hit_count=0, llm_model=None)

        store = get_store(self._index_dir)
        if not store.is_searchable(self._embedding_model):
            logger.info(
                "リソース索引が検索不可（未構築/非互換/モデル不一致）。"
                "DB定義書BM25へフォールバックします。"
            )
            return await AILocationDrafter(corpus_dir=self._corpus_dir).draft(
                items, model=model, recorder=recorder
            )

        # **クエリの embedding を作る前に、索引と同じ重みか照合する**。違えば検索不可と
        # 同じ扱い（DB定義書 BM25 へフォールバック）。ヒント生成（LLM 呼び出し）の前に判定して、
        # 使えない検索のための LLM 呼び出しを無駄にしない。取れなければ未検証として進む。
        embedder = OllamaEmbedder(
            model=self._embedding_model,
            base_url=settings.embedding_host,
            timeout=settings.ollama_timeout,
        )
        identity = await check_embedding_identity(store, embedder)
        if not identity.compatible:
            return await AILocationDrafter(corpus_dir=self._corpus_dir).draft(
                items, model=model, recorder=recorder
            )

        # **検索の前に「院内での呼ばれ方」と想定型を LLM に書かせる**。
        #
        # 項目名＋臨床的意味はマニュアルの言葉で、索引側の DB カラムは略号なので、その
        # ままでは字面が寄らない（略号の列名しか持たない DB 側が負ける）。
        # 生成した語はクエリ文へ足して埋め込み、型は候補の並べ替えの加点に使う。
        #
        # **選択と同じモデルにやらせる。** モデルどうしを比べるとき、
        # 語の生成を1つのモデルに固定して選択だけ別のモデルにさせると比較が交絡する。
        #
        # **失敗しても止めない**（`generate_hints` は例外を投げない）。返らなかった項目は
        # 拡張なし・加点なしになり、この版より前と同じ検索になる。
        selected_model = (model or design_llm_model()).strip()
        # **使ったモデルのダイジェストを残す**。タグだけでは実装が特定できない
        # （同名タグの中身が差し替わっても記録は同じ文字列）。取れなくても生成は続ける
        # ——理由つきの null が残る（`model_digest_of` は例外を投げない）。
        if isinstance(recorder, DesignLlmRecorder):
            await recorder.declare_model_digest(self._client, selected_model)
        hint_result = await generate_hints(self._client, selected_model, items, recorder=recorder)

        # 各項目をクエリ文に整形して一括embedding（同期APIをスレッドへ退避）
        queries = [_query_text(d, hint_result.of(d["link_id"])) for d in items]
        # **短いクエリも引く**。長いクエリでは短い索引テキストが埋没する。
        shorts = [_short_query_text(d, hint_result.of(d["link_id"])) for d in items]
        qvecs = await asyncio.to_thread(embedder.embed, queries)
        svecs = await asyncio.to_thread(embedder.embed, shorts)

        # top-k選択UI: 足切りせず（min_score=0）上位 _TOP_K を提示し、人に選ばせる。
        # 弱い候補も「もっと見る」で見えるが、初期表示は上位3件にフロントで絞る。
        #
        # **規約文書は所在の候補にしない**。kind 横断（db/file/fhir）は設計
        # どおりだが、`file` には `file_role=patient_data`（値が入っている実物）と
        # `file_role=reference`（入力マニュアル等の規約文書）の両方が入る。**規約文書は
        # 意味の典拠であって、データの所在ではない。** 除外していなかったため、項目名で
        # embedding 検索すると項目名をそのまま含むマニュアルの定義文が必ず上位に来て、
        # 略号の列名しか持たない DB 側が負けていた（提示された候補の大半が
        # マニュアルのチャンクで占められた）。
        # 臨床的意味の側が目次を典拠候補から外す `is_citable_reference_unit` に
        # 対応する除外が、所在の側に無かった。
        #
        # **プールを広げて取り、型ヒントで加点してから `_TOP_K` へ絞る**。
        # 提示枠の中だけで並べ替えても、8枠が名前の似た別物で埋まっていれば正しい列は
        # そもそも入ってこない。プロンプトへ載る件数は `_TOP_K` のまま変わらない。
        #
        # **第1段で表を絞ってから、第2段で列を選ぶ**。表のベクトルは
        # 列ベクトルの重心なので索引は作り直していない。第1段が空を返したとき
        # （索引が読めない・表が無い）は絞り込まずに従来どおり全体から取る。
        contexts: list[_ItemContext] = []
        for d, qv, sv in zip(items, qvecs, svecs):
            hint = hint_result.of(d["link_id"])
            query = _query_text(d, hint)
            if not query:
                contexts.append(
                    _ItemContext(
                        link_id=d["link_id"],
                        item_text=d.get("item_text"),
                        label=item_label(d),
                        annotation=d.get("annotation"),
                        hits=[],
                    )
                )
                continue

            # **取得の本体は `retrieve_hits` にある**。`kawasemi index search`
            # も同じ関数を呼ぶ——写した実装を2つ置くと必ずずれる（CLI 側は規約文書の除外も
            # クエリ拡張・加点も入らないまま取り残されていた）。
            hits = retrieve_hits(
                store,
                hint,
                qv,
                sv if _short_query_text(d, hint) else None,
                top_k=_TOP_K,
            )
            contexts.append(
                _ItemContext(
                    link_id=d["link_id"],
                    item_text=d.get("item_text"),
                    label=item_label(d),
                    annotation=d.get("annotation"),
                    hits=hits,
                )
            )

        with_hits = [c for c in contexts if c.hits]
        no_hit_count = len(contexts) - len(with_hits)

        # LLM一推し（cand_id）を取得。候補がある項目だけ提示する。
        #
        # **`llm_model` は「一推しを返したモデル」である**（ヒント生成の導入で壊しかけた契約）。
        # 検索ヒントのコールは候補が0件でも走るが、それは**提案ではない**。ヒントが走った
        # ことを理由にモデル名を出すと、画面と結果が「AI が所在を提案した」と読める形に
        # なる（`test_the_location_route_hides_the_model_name_when_the_llm_was_never_called`）。
        # ヒントのコール自体は来歴（label=`所在検索ヒント`）に残るので、記録は落ちない。
        choice_by_link: dict[str, int | None] = {}
        answered_model: str | None = None
        unattempted: tuple[str, ...] = ()
        duplicated: tuple[str, ...] = ()
        if with_hits:
            # プロンプトへ入れるユニットの中身（snippet）の出所を宣言する（ガード2）。
            # `_build_user_message` が snippet を載せる範囲と一致させる必要があるので、
            # 提示するのと同じ hits から取る。
            if isinstance(recorder, DesignLlmRecorder):
                recorder.declare_content_sources(
                    hit.unit for ctx in with_hits for hit in ctx.hits if hit.unit.snippet
                )
                # **検索に使った索引の同一性も残す**。典拠の付与率は永続化された
                # 典拠から計算するので、下書き時点の索引と測定時点の索引が別物でも値は出る。
                # 索引が使われた瞬間の記録だけが同一性を残せる。
                recorder.declare_index_identity(store)
            answered_model = selected_model
            logger.info(
                f"データ所在AI案生成開始(embedding): 対象{len(with_hits)}件"
                f"（候補なし{no_hit_count}件）, model={selected_model}"
            )
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
                # **完了済みチャンクを捨てない**。**この経路は臨床側と同じ算術が
                # そのまま当てはまる**（同じ対象項目・1コール数十秒・ceil(項目数/4) コール）。しかも
                # 所在候補は**所在の確からしさの段階（rubric）の入力**なので、影響は臨床側より大きい。
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
            # `chosen_cand_id=null` が返った項目はエントリがあるのでここに
            # 入らない（「妥当な候補なし」＝結果であって未試行ではない）。
            unattempted = tuple(c.link_id for c in with_hits if c.link_id not in choice_by_link)

        registry = get_registry()
        drafts: list[SystemLocationDraftItem] = []
        for ctx in contexts:
            # 索引由来の候補を提示順（`_rerank_by_type` が整列済み）で候補化。
            # **`score` の降順とは限らない**。型が一致した候補を加点して
            # 並べ替えるので、★2 が ★3 の上に来ることがある。`score` は素の cosine の
            # ままなので、並びと ★ の単調性は一致しない。
            cands = [self._to_candidate(hit, registry) for hit in ctx.hits]
            # LLM一推しを先頭へ繰り上げ＋recommended。一推し無し（null）なら据え置き。
            chosen = choice_by_link.get(ctx.link_id)
            if chosen is not None:
                rec_idx = next((i for i, h in enumerate(ctx.hits) if h.cand_id == chosen), None)
                if rec_idx is not None:
                    rec = cands.pop(rec_idx)
                    rec.recommended = True
                    cands.insert(0, rec)
            # **診療記録の本文への受け皿を8枠の外へ足す**。
            cands.extend(self._fallback_candidates(store, registry))
            drafts.append(SystemLocationDraftItem(link_id=ctx.link_id, candidates=cands))

        # **`filled` は検索が当てた件数である。** フェイルセーフは常に付くので、これを
        # 数えると全項目が「候補あり」になり、「AI では所在が特定できなかった」件数
        # （臨床側と揃えた内訳）が消える。`fallback` を除いて数える。
        filled = sum(1 for d in drafts if any(not c.fallback for c in d.candidates))
        # **ヒントが何件に効いたかを残す**。`generate_hints` は例外を投げず
        # 縮退するので、**黙って施策抜きで走っていたことに後から気づけない**。「ヒントあり
        # 0件」で完走した記録は、拡張も加点もしていない＝この版より前と同じ検索である。
        #
        # **数えるのは「語か型が1つ以上あった項目」である。** プロンプトは「思いつかなければ
        # 空配列でよい」と言っているので、全項目に空の `hints` を返すモデルがありうる。
        # 応答が返った項目を数えると、検索が素の版と1バイトも変わらないのに全件と出て、
        # **この検出器自身が施策抜きを見逃す**。
        logger.info(
            f"データ所在AI案生成完了(embedding): {filled}件に候補, 候補なし{no_hit_count}件, "
            f"未試行{len(unattempted)}件, 検索ヒントあり{hint_result.effective_count}/{len(items)}件"
            + ("（ヒントの生成に失敗）" if hint_result.failed else "")
        )
        return LocationDraftResult(
            items=drafts,
            no_hit_count=no_hit_count,
            llm_model=answered_model,
            unattempted_link_ids=unattempted,
            duplicated_link_ids=duplicated,
        )

    def _fallback_candidates(
        self, store: ResourceIndexStore, registry: Any
    ) -> list[SystemLocationDraftCandidate]:
        """診療記録の本文への受け皿を作る。

        **検索では診療記録の本文に届きにくい。** 文書種別の単位の `search_text` は
        名称と格納先しか持たないことが多く、登録様式の語彙を含まない。名称に項目の語が
        入っている文書（例: 項目と同じ分野名を持つ報告書）は上位に来うるが、経過記録の
        ような汎用の文書には当たるための語が名称に無い。

        受け皿にする `unit_id` は施設の文書体系で決まるので、`site/location_rules.yaml`
        の `fallback_unit_ids` から読む（`location_rules.py`）。無ければ何も足さない。

        構造化列を持たない項目（文章で記載される所見や経過など）の実際の所在は
        診療記録の本文であり、そこから `llm_extract` で取り出す。**候補が無いより
        「ここを読め」と言えるほうが役に立つ**。

        測定を壊さない形にしてある。

        - **押し出さない。** 上位 `_TOP_K` 件には一切触らず、後ろへ足す。実在の候補が
          押しのけられることは構造上起きない（1表あたりの上限も置いていない。
          候補を減らす仕組みを足さない）
        - **検索由来と区別できる。** `fallback=True` が `proposed_candidates` に残る
        - **人より先に決めない。** LLM の選定が済んだ**後**に足すので LLM は選べず、
          `recommended` が付くことはない。画面も別のブロックへ出す

        **閾値を置かず常に付ける。** 「この項目は難しい」を機械が判定する仕組みを入れると、
        そこが新しい調整対象になり、機械が決めたことに人の名前が付く形へ近づく。
        枠を食わないので常時でも損がない。

        索引に無い `unit_id` は黙って飛ばす（索引を作り直すまでの間も生成を止めない）。

        **検索の候補と同じ足切りを通す**。受け皿は検索を経ないので、
        `_retrieve` の `is_acquirable` が掛からない。以前はここを素通りし、文書が1件も無い
        種別が全項目の提示枠に出続けていた。落ちるのはヘッダが無い種別だけで、本文が
        未連携の種別は検索の側と同じく落とさない。
        """
        out: list[SystemLocationDraftCandidate] = []
        for unit_id in load_location_rules().fallback_unit_ids:
            unit = store.get_unit(unit_id)
            if unit is None or not store.column_fill.is_acquirable(unit):
                continue
            out.append(
                SystemLocationDraftCandidate(
                    source_category=derive_source_category(unit, registry),
                    location=build_location(unit),
                    unit_id=unit.unit_id,
                    connection_id=unit.payload.get("connection_id"),
                    payload=unit.payload,
                    score=None,
                    stars=0,
                    recommended=False,
                    fallback=True,
                )
            )
        return out

    def _to_candidate(self, hit: ResourceHit, registry: Any) -> SystemLocationDraftCandidate:
        """検索ヒットを提示用候補へ変換する（location/種別は payload から決定的導出）。"""
        unit = hit.unit
        return SystemLocationDraftCandidate(
            source_category=derive_source_category(unit, registry),
            location=build_location(unit),
            unit_id=unit.unit_id,
            connection_id=unit.payload.get("connection_id"),
            payload=unit.payload,
            score=hit.score,
            stars=_cosine_stars(hit.score),
        )
