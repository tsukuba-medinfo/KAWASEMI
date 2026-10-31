"""`kawasemi index search` の検索ロジック（search_cli）のオフラインテスト

実Ollama に依存しない。BM25 フォールバック経路は既存フィクスチャ
（tests/fixtures/db_definition/L2/column_index.jsonl）で実検索し、embedding 経路は
get_store / OllamaEmbedder をスタブして ResourceHit→SearchHit の写像と診断を検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from kawasemi.ai_input.db_definition import reset_index_cache
from kawasemi.ai_input.resource_index import search_cli
from kawasemi.ai_input.resource_index.schema import BuildInfo, Kind, ResourceHit, ResourceUnit
from kawasemi.ai_input.resource_index.search_cli import (
    SearchResult,
    format_json,
    format_text,
    run_search,
)

FIXTURE_CORPUS = str(Path(__file__).resolve().parents[1] / "fixtures" / "db_definition")


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    reset_index_cache()


# --- BM25 フォールバック経路（オフライン実検索） ---------------------------------


def test_bm25_engine_returns_hits_from_corpus() -> None:
    result = run_search("患者", engine="bm25", corpus_dir=FIXTURE_CORPUS, top_k=5)
    assert result.effective_engine == "bm25"
    assert result.requested_engine == "bm25"
    assert result.used_fallback is False  # 明示指定はフォールバック扱いにしない
    assert result.error is None
    assert result.hits, "コーパスから候補が返るはず"
    top = result.hits[0]
    assert top.rank == 1
    assert top.cand_id == 0
    assert top.kind == Kind.DB
    assert top.location  # build_location が組み立てられる
    assert top.source_category in {"local_db", "external_db"}


def test_auto_falls_back_to_bm25_when_index_missing(tmp_path: Path) -> None:
    # 未配置の索引ディレクトリ → searchable=False → auto は BM25 に落ちる
    empty_dir = str(tmp_path / "no_index")
    result = run_search(
        "患者", engine="auto", index_dir=empty_dir, corpus_dir=FIXTURE_CORPUS, top_k=5
    )
    assert result.effective_engine == "bm25"
    assert result.used_fallback is True
    assert result.diagnostics["searchable"] is False
    assert any("未配置" in r for r in result.diagnostics["not_searchable_reasons"])
    assert result.hits


def test_annotation_is_concatenated_into_query() -> None:
    result = run_search("発症日", annotation="脳血管障害", engine="bm25", corpus_dir=FIXTURE_CORPUS)
    assert result.query == "発症日 脳血管障害"


def test_empty_query_is_error() -> None:
    result = run_search("", engine="bm25", corpus_dir=FIXTURE_CORPUS)
    assert result.error == "クエリが空です"
    assert result.hits == []


# --- embedding 経路（get_store / OllamaEmbedder をスタブ） -----------------------


class _FakeColumnFill:
    def is_acquirable(self, unit: object) -> bool:
        return True


class _FakeStore:
    """is_searchable=True を満たす最小ストア。search は固定ヒットを返す。

    **本番の取得（`resource_drafter.retrieve_hits`）が呼ぶ口をすべて持つ**。
    CLI が本番と同じ関数を通るので、`predicate`（規約文書の除外）や
    `search_tables`（第1段の表の絞り込み）、`column_fill`（空の列の除外）を
    受け取れないスタブでは動かない。
    """

    def __init__(self, hits: list[ResourceHit]) -> None:
        self._hits = hits
        self.index_dir = Path("/fake/index")
        self.is_loaded = True
        self.schema_compatible = True
        self.unit_count = len(hits)
        self.embedding_model = "bge-m3"
        self.build_info = BuildInfo(embedding_model="bge-m3", dim=4)
        self.embedding_digest = None  # 未検証
        self.column_fill = _FakeColumnFill()

    def is_searchable(self, model: str) -> bool:
        return model == self.embedding_model

    def search_tables(self, query_vector: np.ndarray, top_n: int) -> list[str]:
        return []

    def label_count(self, label: str | None) -> int:
        # 定型列の抑制に使う。スタブでは常に1件＝抑制しない。
        return 1

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 8,
        min_score: float = 0.0,
        predicate: object = None,
    ) -> list[ResourceHit]:
        hits = self._hits
        if callable(predicate):
            hits = [h for h in hits if predicate(h.unit)]
        return hits[:top_k]


class _FakeEmbedder:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.ones((len(texts), 4), dtype=np.float32)


def _db_hit() -> ResourceHit:
    unit = ResourceUnit(
        unit_id="db:DEMO.LAB.WBC",
        kind=Kind.DB,
        label="白血球数",
        search_text="白血球数 WBC",
        snippet="DB=DEMO 表=LAB",
        payload={
            "connection_id": "DEMO_DB",
            "db_qualified": "DEMO.LAB",
            "physical_name": "WBC",
            "logical_name": "白血球数",
        },
    )
    return ResourceHit(cand_id=0, unit=unit, score=0.91)


def test_embedding_engine_maps_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore([_db_hit()])
    monkeypatch.setattr(search_cli, "get_store", lambda index_dir=None: store)
    monkeypatch.setattr("kawasemi.ai_input.resource_index.embedder.OllamaEmbedder", _FakeEmbedder)

    result = run_search("白血球数", engine="auto", model="bge-m3", use_hints=False)
    assert result.effective_engine == "embedding"
    assert result.used_fallback is False
    assert result.diagnostics["searchable"] is True
    assert result.diagnostics["query_dim"] == 4
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.kind == Kind.DB
    assert hit.unit_id == "db:DEMO.LAB.WBC"
    assert hit.connection_id == "DEMO_DB"
    assert "WBC" in hit.location
    assert hit.source_category in {"local_db", "external_db"}


class _DigestEmbedder(_FakeEmbedder):
    """クエリ側のダイジェストを名乗る embedder。"""

    digest_value: str | None = None

    def digest(self) -> str | None:
        return type(self).digest_value


def _run_with_digests(
    monkeypatch: pytest.MonkeyPatch, index_digest: str | None, query_digest: str | None
):
    store = _FakeStore([_db_hit()])
    store.embedding_digest = index_digest
    monkeypatch.setattr(search_cli, "get_store", lambda index_dir=None: store)
    _DigestEmbedder.digest_value = query_digest
    monkeypatch.setattr("kawasemi.ai_input.resource_index.embedder.OllamaEmbedder", _DigestEmbedder)
    return run_search("白血球数", engine="embedding", model="bge-m3", use_hints=False)


def test_a_different_embedding_digest_stops_the_cli_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """本番と同じく、索引とクエリの embedding の重みが違えば検索しない。

    診断にはダイジェストと照合の結果を出す（なぜ0件なのかを説明できる）。
    """
    result = _run_with_digests(monkeypatch, "aa" * 32, "bb" * 32)

    assert result.hits == [], "別の重みなのに検索結果を返している"
    assert result.error and "重み" in result.error
    assert result.diagnostics["index_embedding_digest"] == "aa" * 32
    assert result.diagnostics["query_embedding_digest"] == "bb" * 32


@pytest.mark.parametrize(
    ("index_digest", "query_digest", "note"),
    [("aa" * 32, "aa" * 32, "一致"), (None, "bb" * 32, "未検証"), ("aa" * 32, None, "未検証")],
    ids=["same", "index-unverified", "query-unverified"],
)
def test_a_matching_or_unverifiable_digest_lets_the_cli_search_proceed(
    monkeypatch: pytest.MonkeyPatch, index_digest: str | None, query_digest: str | None, note: str
) -> None:
    """一致、または比べられないときは従来どおり検索する（**肯定側も測る**）。"""
    result = _run_with_digests(monkeypatch, index_digest, query_digest)

    assert result.error is None
    assert len(result.hits) == 1
    assert note in result.diagnostics["embedding_identity"]


def test_embedding_model_mismatch_reason_in_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore([_db_hit()])
    monkeypatch.setattr(search_cli, "get_store", lambda index_dir=None: store)
    # 索引は bge-m3 だが別モデルを指定 → searchable=False → auto は BM25 へ
    result = run_search("白血球数", engine="auto", model="other-model", corpus_dir=FIXTURE_CORPUS)
    assert result.effective_engine == "bm25"
    assert result.used_fallback is True
    assert any("モデル不一致" in r for r in result.diagnostics["not_searchable_reasons"])


def test_forced_embedding_without_index_is_error(tmp_path: Path) -> None:
    result = run_search("白血球数", engine="embedding", index_dir=str(tmp_path / "none"))
    assert result.effective_engine == "embedding"
    assert result.error is not None
    assert "embedding" in result.error


# --- フォーマッタ ---------------------------------------------------------------


def test_format_text_contains_header_and_hits() -> None:
    result = run_search("患者", engine="bm25", corpus_dir=FIXTURE_CORPUS, top_k=3)
    text = format_text(result)
    assert "クエリ:" in text
    assert "経路" in text
    assert "bm25" in text


def test_format_json_is_valid_and_roundtrips() -> None:
    result = run_search("患者", engine="bm25", corpus_dir=FIXTURE_CORPUS, top_k=3)
    payload = json.loads(format_json(result))
    assert payload["effective_engine"] == "bm25"
    assert "diagnostics" in payload
    assert isinstance(payload["hits"], list)


def test_format_text_no_hits_message() -> None:
    result = SearchResult(
        query="zzz",
        requested_engine="bm25",
        effective_engine="bm25",
        used_fallback=False,
    )
    assert "ヒットなし" in format_text(result)


# --------------------------------------------------------------------------- #
# 本番の所在探索との一致
#
# このコマンドは「なぜこの列が提案された／されなかった」を説明するための道具なので、
# 本番と別の順位を出すと調査の結論が変わる。以前は CLI 側に別の検索が書かれており、
# 規約文書の除外もクエリ拡張・型加点も入っていなかった。
# **写した実装を2つ置くと必ずずれる**ので、同じ関数を通すこと自体を固定する。
# --------------------------------------------------------------------------- #


def test_the_cli_calls_the_same_retrieval_as_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI の embedding 経路が `resource_drafter.retrieve_hits` を呼ぶこと。

    ここを通らない実装（`store.search` の直呼び）へ戻すと、規約文書の除外も
    型加点も第1段の表の絞り込みも RRF 併合も落ちる。
    """
    import kawasemi.ai_input.resource_drafter as drafter_mod

    called: dict[str, object] = {}
    original = drafter_mod.retrieve_hits

    def _spy(store, hint, qvec, svec, *, top_k=8):  # type: ignore[no-untyped-def]
        called["hint"] = hint
        called["top_k"] = top_k
        called["has_short"] = svec is not None
        return original(store, hint, qvec, svec, top_k=top_k)

    monkeypatch.setattr(drafter_mod, "retrieve_hits", _spy)
    store = _FakeStore([_db_hit()])
    monkeypatch.setattr(search_cli, "get_store", lambda index_dir=None: store)
    monkeypatch.setattr("kawasemi.ai_input.resource_index.embedder.OllamaEmbedder", _FakeEmbedder)

    result = run_search(
        "白血球数",
        annotation="末梢血の白血球数",
        engine="embedding",
        model="bge-m3",
        top_k=5,
        hint_terms=["WBC", "白血球"],
        hint_types=["数値"],
    )

    assert called, "retrieve_hits を通っていない（本番と別の検索をしている）"
    assert called["top_k"] == 5
    # 項目名があるので短いクエリも引く（RRF 併合が効く）
    assert called["has_short"] is True
    hint = called["hint"]
    assert hint.terms == ("WBC", "白血球")
    assert hint.expected_types == ("数値",)
    assert len(result.hits) == 1


def test_the_query_is_expanded_with_the_hint_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    """院内での呼ばれ方がクエリ文の**後ろへ**足されること（クエリ拡張）。

    足されないと、略号の列名しか持たない DB 側が項目名だけでは当たらない。
    """
    store = _FakeStore([_db_hit()])
    monkeypatch.setattr(search_cli, "get_store", lambda index_dir=None: store)
    monkeypatch.setattr("kawasemi.ai_input.resource_index.embedder.OllamaEmbedder", _FakeEmbedder)

    result = run_search(
        "白血球数",
        annotation="末梢血",
        engine="embedding",
        model="bge-m3",
        hint_terms=["WBC"],
    )

    expanded = result.diagnostics["expanded_query"]
    assert expanded == "白血球数 末梢血 WBC", expanded
    # 短いクエリは臨床的意味を外す
    assert result.diagnostics["short_query"] == "白血球数 WBC"
    assert result.diagnostics["hint_terms"] == ["WBC"]


def test_reference_documents_are_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    """規約文書（`file_role=reference`）が候補に出ないこと。

    出ると、項目名をそのまま含むマニュアルの定義文が上位を埋め、略号の列名しか持たない
    DB 側が負ける（実際の索引では提示候補の大半がマニュアルのチャンクだった）。
    """
    reference = ResourceHit(
        cand_id=1,
        unit=ResourceUnit(
            unit_id="file:manual#1",
            kind=Kind.FILE,
            label="白血球数の定義",
            search_text="白血球数",
            snippet="マニュアルの定義文",
            payload={"file_role": "reference", "path": "manual.pdf"},
        ),
        score=0.99,
    )
    store = _FakeStore([reference, _db_hit()])
    monkeypatch.setattr(search_cli, "get_store", lambda index_dir=None: store)
    monkeypatch.setattr("kawasemi.ai_input.resource_index.embedder.OllamaEmbedder", _FakeEmbedder)

    result = run_search("白血球数", engine="embedding", model="bge-m3", use_hints=False)

    unit_ids = [h.unit_id for h in result.hits]
    assert "file:manual#1" not in unit_ids, unit_ids
    assert "db:DEMO.LAB.WBC" in unit_ids


def test_hints_are_not_generated_when_the_index_is_unusable(tmp_path: Path) -> None:
    """索引が読めないときに LLM を呼ばないこと。

    生成は推論サーバへの往復なので、索引の無い環境では「結局エラーになる検索」のために
    上限まで待つことになる（実測 37 秒）。本番は `is_searchable` を通ってから生成に入る。
    """
    result = run_search(
        "白血球数", engine="embedding", index_dir=str(tmp_path / "missing"), model="bge-m3"
    )

    assert result.error is not None
    assert result.diagnostics.get("hint_source") in (None, "なし（--no-hints）")
