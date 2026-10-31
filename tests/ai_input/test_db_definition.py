"""`kawasemi.ai_input.db_definition`（DB定義書コーパスの BM25 検索層）のユニットテスト

検証対象:
- `_tokenize`: NFKC/lower 正規化、ascii-token、非asciiの char-bigram
- `ColumnHit.to_candidate` / `build_location`
- `DbDefinitionIndex.load`: 正常 / 未配置 / 空行スキップ / 破損JSON
- `DbDefinitionIndex.search`: 空インデックス / 空クエリ / length0ドキュメント / 関連順
- `get_index` / `reset_index_cache`: コーパス単位の遅延ロードキャッシュ
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kawasemi.ai_input.db_definition import (
    ColumnHit,
    DbDefinitionIndex,
    _tokenize,
    get_index,
    reset_index_cache,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    """モジュール内シングルトンのキャッシュをテスト間で汚染させない。"""
    reset_index_cache()
    yield
    reset_index_cache()


def _write_corpus(corpus_dir: Path, columns: list[dict] | str) -> Path:
    """corpus_dir/L2/column_index.jsonl を生成する。columns に str を渡すと生文字列を書く。"""
    l2 = corpus_dir / "L2"
    l2.mkdir(parents=True, exist_ok=True)
    path = l2 / "column_index.jsonl"
    if isinstance(columns, str):
        path.write_text(columns, encoding="utf-8")
    else:
        path.write_text(
            "\n".join(json.dumps(c, ensure_ascii=False) for c in columns) + "\n",
            encoding="utf-8",
        )
    return path


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------


def test_tokenize_empty() -> None:
    assert _tokenize("") == []


def test_tokenize_ascii_tokens_lowercased() -> None:
    assert _tokenize("Hello_World 123") == ["hello_world", "123"]


def test_tokenize_nfkc_fullwidth_to_ascii() -> None:
    # 全角英数は NFKC で半角化され ascii トークンになる
    assert _tokenize("ＡＢ12") == ["ab12"]


def test_tokenize_single_nonascii_char() -> None:
    # 非asciiが1文字のときは bigram 化せずそのまま1トークン
    assert _tokenize("血") == ["血"]


def test_tokenize_nonascii_bigrams() -> None:
    assert _tokenize("芽球性") == ["芽球", "球性"]


def test_tokenize_mixed_ascii_and_nonascii() -> None:
    tokens = _tokenize("blast 芽球")
    assert "blast" in tokens
    assert "芽球" in tokens


# ---------------------------------------------------------------------------
# ColumnHit
# ---------------------------------------------------------------------------


def test_column_hit_to_candidate() -> None:
    hit = ColumnHit(
        cand_id=3,
        db_group="EHR",
        db_qualified="dbo.lab",
        table_logical="検査",
        physical_name="wbc",
        logical_name="白血球数",
        type_raw="int",
        pk=True,
        notes="メモ",
        score=1.23,
    )
    cand = hit.to_candidate()
    assert cand["cand_id"] == 3
    assert cand["type"] == "int"  # type_raw が type にマップされる
    assert cand["physical_name"] == "wbc"
    assert cand["pk"] is True
    assert "score" not in cand  # 候補dictにスコアは含めない


def test_column_hit_build_location_full() -> None:
    hit = ColumnHit(
        cand_id=0,
        db_group="EHR",
        db_qualified="dbo.lab",
        table_logical="検査",
        physical_name="wbc",
        logical_name="白血球数",
        type_raw="int",
        pk=False,
        notes=None,
    )
    assert hit.build_location() == "DB: EHR / 表: dbo.lab（検査） / カラム: wbc（白血球数）"


def test_column_hit_build_location_defaults() -> None:
    hit = ColumnHit(
        cand_id=0,
        db_group=None,
        db_qualified=None,
        table_logical=None,
        physical_name=None,
        logical_name=None,
        type_raw=None,
        pk=False,
        notes=None,
    )
    assert hit.build_location() == "DB: （DB不明） / 表: （表不明） / カラム: （カラム不明）"


# ---------------------------------------------------------------------------
# DbDefinitionIndex.load
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    index = DbDefinitionIndex.load(tmp_path)
    assert index.is_loaded is False
    assert index.column_count == 0
    assert index.search("白血球") == []


def test_load_valid_corpus(tmp_path: Path) -> None:
    _write_corpus(
        tmp_path,
        [
            {"physical_name": "wbc", "logical_name": "白血球数", "db_group": "EHR"},
            {"physical_name": "hgb", "logical_name": "ヘモグロビン", "db_group": "EHR"},
        ],
    )
    index = DbDefinitionIndex.load(tmp_path)
    assert index.is_loaded is True
    assert index.column_count == 2


def test_load_skips_blank_lines(tmp_path: Path) -> None:
    raw = (
        json.dumps({"physical_name": "wbc", "logical_name": "白血球数"}, ensure_ascii=False)
        + "\n\n   \n"
        + json.dumps({"physical_name": "hgb", "logical_name": "ヘモグロビン"}, ensure_ascii=False)
        + "\n"
    )
    _write_corpus(tmp_path, raw)
    index = DbDefinitionIndex.load(tmp_path)
    assert index.column_count == 2  # 空行は数えない


def test_load_corrupt_json_returns_empty(tmp_path: Path) -> None:
    _write_corpus(tmp_path, '{"physical_name": "wbc"}\n{壊れたJSON\n')
    index = DbDefinitionIndex.load(tmp_path)
    # 壊れた行に当たったら空インデックス扱い（部分ロードしない）
    assert index.is_loaded is False
    assert index.column_count == 0


# ---------------------------------------------------------------------------
# DbDefinitionIndex.search
# ---------------------------------------------------------------------------


def test_search_empty_index_returns_empty() -> None:
    assert DbDefinitionIndex().search("白血球") == []


def test_search_empty_query_returns_empty(tmp_path: Path) -> None:
    _write_corpus(tmp_path, [{"physical_name": "wbc", "logical_name": "白血球数"}])
    index = DbDefinitionIndex.load(tmp_path)
    # 記号のみ → トークンが出ず候補ゼロ
    assert index.search("!!!") == []


def test_search_excludes_empty_columns(tmp_path: Path) -> None:
    # 全フィールド空のカラム（length 0）は候補に出ない。
    # 注: length0 の early-continue と後段の score>0 フィルタの双方で除外されるため、
    # 本テストは「空カラムが結果に出ない」挙動を保証するもので、片方の分岐単独の検証ではない。
    _write_corpus(
        tmp_path,
        [
            {"physical_name": "wbc", "logical_name": "白血球数"},
            {},  # _doc_text が空 → length 0
        ],
    )
    index = DbDefinitionIndex.load(tmp_path)
    assert index.column_count == 2
    hits = index.search("白血球")
    assert len(hits) == 1
    assert hits[0].physical_name == "wbc"


def test_search_ranks_relevant_column_first(tmp_path: Path) -> None:
    _write_corpus(
        tmp_path,
        [
            {"physical_name": "wbc", "logical_name": "白血球数", "db_group": "EHR"},
            {"physical_name": "hgb", "logical_name": "ヘモグロビン", "db_group": "EHR"},
            {"physical_name": "dx", "logical_name": "病名", "db_group": "EHR"},
        ],
    )
    index = DbDefinitionIndex.load(tmp_path)
    hits = index.search("白血球")
    assert hits
    assert hits[0].cand_id == 0  # スコア降順で cand_id が振られる
    assert hits[0].physical_name == "wbc"
    assert hits[0].score > 0


def test_search_respects_top_k(tmp_path: Path) -> None:
    _write_corpus(
        tmp_path,
        [{"physical_name": f"c{i}", "logical_name": "白血球数"} for i in range(10)],
    )
    index = DbDefinitionIndex.load(tmp_path)
    hits = index.search("白血球", top_k=3)
    assert len(hits) == 3


# ---------------------------------------------------------------------------
# get_index / reset_index_cache
# ---------------------------------------------------------------------------


def test_get_index_caches_per_corpus(tmp_path: Path) -> None:
    _write_corpus(tmp_path, [{"physical_name": "wbc", "logical_name": "白血球数"}])
    first = get_index(str(tmp_path))
    second = get_index(str(tmp_path))
    assert first is second  # 同一コーパスはキャッシュされ同じインスタンス


def test_reset_index_cache_forces_reload(tmp_path: Path) -> None:
    _write_corpus(tmp_path, [{"physical_name": "wbc", "logical_name": "白血球数"}])
    first = get_index(str(tmp_path))
    reset_index_cache()
    second = get_index(str(tmp_path))
    assert first is not second  # リセット後は再ロードされ別インスタンス
