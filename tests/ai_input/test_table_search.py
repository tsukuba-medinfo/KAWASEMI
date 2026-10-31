"""表のベクトルと2段階検索のユニットテスト

索引は列ごとに1本のベクトルしか持たず、その1本に「どの表か」と「その表のどの列か」の
両方を負わせている。そのため、名前に領域語（例「病理」）を含む列数の少ない表
（採番管理の表で、中身は年・月・日）が、列数が多く中身が合う表よりコサイン上位に
来て、後者が `_TOP_K=8` の枠に入らないことがある。

ここで測るのは3つ。

1. 表のベクトルが**列ベクトルの重心**であり、索引を作り直していないこと
2. **中身が合う表が、名前だけ似ている表より上に来る**こと（第1段の目的）
3. 第1段が正しい表を落としたときに**列が到達不能にならない**こと（フォールバック）

3 が最も重要である。絞り込みは「LLM が提案しなかった所在」の件数を悪化させうる
ので、`_TYPE_BOOST` をハードフィルタにしなかったのと同じ理由で逃げ道を残す。
"""

from __future__ import annotations

import numpy as np
import pytest

from kawasemi.ai_input.resource_index.column_fill import ColumnFillIndex
from kawasemi.ai_input.resource_index.schema import Kind, ResourceUnit
from kawasemi.ai_input.resource_index.store import ResourceIndexStore


def _unit(kind: str, uid: str, *, conn: str | None = None, tbl: str | None = None) -> ResourceUnit:
    payload: dict[str, object] = {}
    if conn:
        payload["connection_id"] = conn
    if tbl:
        payload["db_qualified"] = tbl
    return ResourceUnit(unit_id=uid, kind=kind, label=uid, search_text=uid, payload=payload)


def _rerank(hits: list, top_k: int = 8) -> list:
    """型ヒント無しで提示枠を作る（並びは索引が返した順のまま）。"""
    from kawasemi.ai_input import resource_drafter as rd_mod
    from kawasemi.ai_input.location_hint import EMPTY_HINT

    return rd_mod._rerank_by_type(hits, EMPTY_HINT, top_k)


def _store(units: list[ResourceUnit], vectors: list[list[float]]) -> ResourceIndexStore:
    """行列を直接差し込んだストアを作る（索引ファイルを介さない）。"""
    st = ResourceIndexStore()
    st._units = units
    m = np.asarray(vectors, dtype=np.float32)
    st._matrix = m / np.linalg.norm(m, axis=1, keepdims=True)
    return st


# --------------------------------------------------------------------------- #
# 表キー
# --------------------------------------------------------------------------- #


def test_only_db_units_belong_to_a_table():
    """表を持つのは `kind=db` だけ。

    ファイル・FHIR・マスタは表の2段階検索の対象外で、第1段で落とさない。所在の候補
    としては残す必要がある（患者データのファイルは正当な所在である）。
    """
    col = _unit(Kind.DB, "db:T.C", conn="dwh", tbl="DWH.病理所見記録")
    assert ResourceIndexStore.table_key(col) == ("dwh", "DWH.病理所見記録")
    assert ResourceIndexStore.table_key(_unit(Kind.FILE, "file:x")) is None
    assert ResourceIndexStore.table_key(_unit(Kind.FHIR, "fhir:x")) is None
    assert ResourceIndexStore.table_key(_unit(Kind.MASTER, "master:x", conn="A", tbl="B")) is None
    # 接続や表名が欠けたカラムは表に紐づけない（重心の分母を壊さない）
    assert ResourceIndexStore.table_key(_unit(Kind.DB, "db:x", conn="A")) is None


# --------------------------------------------------------------------------- #
# 重心
# --------------------------------------------------------------------------- #


def test_the_table_vector_is_the_normalized_centroid_of_its_columns():
    """表のベクトルが列ベクトルの重心であること。

    **索引を作り直さない**ことがこの設計の要点である。`embeddings.npy` から決定的に
    導出されるので、索引の同一性も `build_info` も動かない。
    """
    units = [
        _unit(Kind.DB, "db:A.1", conn="C", tbl="A"),
        _unit(Kind.DB, "db:A.2", conn="C", tbl="A"),
        _unit(Kind.DB, "db:B.1", conn="C", tbl="B"),
    ]
    st = _store(units, [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]])
    st._ensure_table_matrix()
    assert st._table_keys == [("C", "A"), ("C", "B")]
    # 表Aは (1,0) と (0,1) の重心 → 正規化して (0.707, 0.707)
    assert st._table_matrix is not None
    np.testing.assert_allclose(st._table_matrix[0], [0.7071, 0.7071], atol=1e-3)
    np.testing.assert_allclose(st._table_matrix[1], [1.0, 0.0], atol=1e-3)


def test_a_table_whose_contents_match_beats_one_whose_name_merely_looks_alike():
    """**中身が合う表が、名前だけ似ている表より上に来る**こと（2段階検索の目的）。

    よくある構図を最小で再現する。病理の採番管理の表は列数が少なく中身は
    年・月・日、`病理所見記録` は列数が多く中身が病理である。列ごとのベクトルで引くと
    前者が上に来ていた（短いテキストに「病理」が凝縮されるため）。表の重心で引けば
    後者が上に来る。
    """
    # 次元0 = 「病理の中身」、次元1 = 「採番・日付の中身」
    units = [_unit(Kind.DB, f"db:採番.{i}", conn="SRC", tbl="採番管理") for i in range(7)]
    vecs = [[0.1, 1.0] for _ in range(7)]  # 中身は日付だけ
    units += [_unit(Kind.DB, f"db:病理.{i}", conn="DWH", tbl="病理所見記録") for i in range(20)]
    vecs += [[1.0, 0.1] for _ in range(20)]  # 中身は病理
    st = _store(units, vecs)

    ranked = st.search_tables(np.array([1.0, 0.0], dtype=np.float32), top_n=2)
    assert ranked[0] == ("DWH", "病理所見記録"), "中身が合う表が第1段の1位に来るべき"


def test_an_empty_index_returns_no_tables():
    """表が1つも無ければ空を返す（呼び出し側は「絞り込まない」と解釈する）。"""
    st = _store([_unit(Kind.FILE, "file:x")], [[1.0, 0.0]])
    assert st.search_tables(np.array([1.0, 0.0], dtype=np.float32), top_n=5) == []
    assert ResourceIndexStore().search_tables(np.array([1.0]), top_n=5) == []


def test_top_n_zero_or_negative_returns_no_tables():
    st = _store([_unit(Kind.DB, "db:A.1", conn="C", tbl="A")], [[1.0, 0.0]])
    assert st.search_tables(np.array([1.0, 0.0], dtype=np.float32), top_n=0) == []


# --------------------------------------------------------------------------- #
# 2段階（呼び出し側）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_second_stage_is_restricted_to_the_first_stage_tables(monkeypatch, tmp_path):
    """第2段が第1段で残した表だけを見ること（結線）。"""
    from kawasemi.ai_input import resource_drafter as rd_mod

    seen: dict = {}

    class _Store:
        # 充填状況の成果物は任意。**空にすると足切りをしない**ので、この二重では
        # これまでの挙動がそのまま出る（`column_fill.ColumnFillIndex` の縮退）。
        column_fill = ColumnFillIndex()
        # ダイジェストを持たない索引（未検証。検索は無効化されない）
        embedding_digest = None

        def is_searchable(self, model):  # noqa: ANN001, ARG002
            return True

        def label_count(self, label):  # noqa: ANN001, ARG002
            return 1

        def get_unit(self, unit_id):  # noqa: ANN001, ARG002
            # 受け皿は索引に単位が無ければ黙って飛ばす。この二重は索引を持たない。
            return None

        def search_tables(self, qv, top_n):  # noqa: ANN001, ARG002
            seen["top_n"] = top_n
            return [("DWH", "病理所見記録")]

        def search(self, qv, top_k, min_score, predicate=None):  # noqa: ANN001, ARG002
            from kawasemi.ai_input.resource_index.schema import ResourceHit

            seen.setdefault("predicates", []).append(predicate)
            # **絞り込みだけで提示枠が埋まる量を返す。** 枠に届かないとフォールバックが
            # 働いて全体検索が混ざるので、絞り込みそのものを見るには枠を埋める必要がある。
            units = [
                _unit(Kind.DB, f"db:病理.{i}", conn="DWH", tbl="病理所見記録")
                for i in range(rd_mod._TOP_K)
            ]
            units.append(_unit(Kind.DB, "db:採番.1", conn="SRC", tbl="採番管理"))
            units.append(_unit(Kind.FILE, "file:rep"))
            kept = [u for u in units if predicate is None or predicate(u)]
            return [ResourceHit(unit=u, score=0.6, cand_id=i) for i, u in enumerate(kept)]

    monkeypatch.setattr(rd_mod, "get_store", lambda _dir: _Store())
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: _Embedder())
    drafter = rd_mod.AIResourceDrafter(index_dir=str(tmp_path))
    drafter._embedding_model = "bge-m3"
    _silent_llm(drafter)

    result = await drafter.draft([{"link_id": "q1", "item_text": "pT分類", "annotation": None}])

    assert seen["top_n"] == rd_mod._TABLE_TOP_N
    ids = [c.unit_id for c in result.items[0].candidates]
    assert "db:病理.1" in ids, "第1段で残した表の列は候補に入る"
    assert "db:採番.1" not in ids, "第1段で落とした表の列は候補から外れる"
    # 表を持たない種別（ファイル・FHIR・マスタ）は第1段で落とさない。ここでは提示枠が
    # DB の列で埋まるので `file:rep` は入らないが、predicate は通していることを見る。
    pred = seen["predicates"][0]
    assert pred(_unit(Kind.FILE, "file:rep")) is True, "表を持たない種別を落としてはいけない"


@pytest.mark.asyncio
async def test_a_starved_second_stage_falls_back_to_the_whole_index(monkeypatch, tmp_path):
    """**第1段が外したときに列を到達不能にしない**。

    絞り込みの結果が提示枠に届かなければ、全体検索から補う。これが無いと、第1段が
    正しい表を落とした項目は候補ゼロになり、「LLM が提案しなかった所在」の
    件数が悪化する。加点をハードフィルタにしなかったのと同じ理由である。
    """
    from kawasemi.ai_input import resource_drafter as rd_mod
    from kawasemi.ai_input.resource_index.schema import ResourceHit

    class _Store:
        # 充填状況の成果物は任意。**空にすると足切りをしない**ので、この二重では
        # これまでの挙動がそのまま出る（`column_fill.ColumnFillIndex` の縮退）。
        column_fill = ColumnFillIndex()
        # ダイジェストを持たない索引（未検証。検索は無効化されない）
        embedding_digest = None

        def is_searchable(self, model):  # noqa: ANN001, ARG002
            return True

        def label_count(self, label):  # noqa: ANN001, ARG002
            return 1

        def get_unit(self, unit_id):  # noqa: ANN001, ARG002
            # 受け皿は索引に単位が無ければ黙って飛ばす。この二重は索引を持たない。
            return None

        def search_tables(self, qv, top_n):  # noqa: ANN001, ARG002
            return [("DWH", "無関係な表")]  # 正しい表を落とした

        def search(self, qv, top_k, min_score, predicate=None):  # noqa: ANN001, ARG002
            units = [
                _unit(Kind.DB, f"db:他.{i}", conn="SRC", tbl=f"表{i}") for i in range(rd_mod._TOP_K)
            ]
            kept = [u for u in units if predicate is None or predicate(u)]
            return [ResourceHit(unit=u, score=0.6, cand_id=i) for i, u in enumerate(kept)]

    monkeypatch.setattr(rd_mod, "get_store", lambda _dir: _Store())
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: _Embedder())
    drafter = rd_mod.AIResourceDrafter(index_dir=str(tmp_path))
    drafter._embedding_model = "bge-m3"
    _silent_llm(drafter)

    result = await drafter.draft([{"link_id": "q1", "item_text": "pT分類", "annotation": None}])
    cands = result.items[0].candidates
    assert len(cands) == rd_mod._TOP_K, "絞り込みで枯れたら全体検索から補うべき"


class _Embedder:
    model = "bge-m3"

    def embed(self, texts):  # noqa: ANN001
        return [np.array([1.0, 0.0], dtype=np.float32) for _ in texts]


def _silent_llm(drafter) -> None:  # noqa: ANN001
    """LLM を「ヒント空・一推しなし」で答えさせる（検索経路だけを見る）。"""
    import json
    import types

    async def chat(**kwargs):
        fmt = kwargs.get("format") or {}
        body = (
            {"hints": []}
            if "hints" in fmt.get("properties", {})
            else {"choices": [{"link_id": "q1", "chosen_cand_id": None}]}
        )
        return types.SimpleNamespace(
            message=types.SimpleNamespace(content=json.dumps(body, ensure_ascii=False))
        )

    drafter._client = types.SimpleNamespace(chat=chat)


def test_the_slate_follows_score_order_without_a_per_table_cap():
    """1表あたりの上限を**置かない**こと。

    上限で押し出された列が正しい列でないとは言えない。**押し出されたのが正しい列かは
    分からない**ので、人の確定作業の前に候補を減らす仕組みは置かない。

    本テストは上限が黙って戻ることへの歯止めである。同じ表の列がスコア順に並んで
    いれば、**そのまま8件とも提示される**。
    """
    from kawasemi.ai_input.resource_index.schema import ResourceHit

    hits = [
        ResourceHit(
            cand_id=i,
            score=0.60 - i * 0.001,
            unit=_unit(Kind.DB, f"db:同じ表.{i}", conn="C", tbl="同じ表"),
        )
        for i in range(8)
    ]
    hits += [
        ResourceHit(
            cand_id=8 + i,
            score=0.55 - i * 0.001,
            unit=_unit(Kind.DB, f"db:別表{i}.1", conn="C", tbl=f"別表{i}"),
        )
        for i in range(5)
    ]
    out = _rerank(hits)
    tables = [ResourceIndexStore.table_key(h.unit) for h in out]
    assert len(out) == 8
    assert tables.count(("C", "同じ表")) == 8, "スコア順のまま。表で間引かない"
    assert [h.cand_id for h in out] == list(range(8)), "cand_id は振り直す"


def test_the_slate_keeps_units_that_have_no_table():
    """表を持たない種別も素通しであること。"""
    from kawasemi.ai_input.resource_index.schema import ResourceHit

    hits = [ResourceHit(cand_id=i, score=0.6, unit=_unit(Kind.FILE, f"file:{i}")) for i in range(6)]
    assert len(_rerank(hits)) == 6


def test_boilerplate_columns_are_pushed_down_by_their_index_frequency():
    """同じ label が索引に何度も出る列を下げること。

    `施設コード`(`FACILITY_CODE`) や `診療科コード` は多くの表に同じ物理名と論理名で
    入っていて索引に数百回出る。固有の列は1回しか出ない。「〜コード」を問う項目の
    提示枠を定型列が占めていた。

    **cosine が同じなら、索引に1回しか出ない列が上に来る。**
    """
    from kawasemi.ai_input import resource_drafter as rd_mod
    from kawasemi.ai_input.location_hint import EMPTY_HINT
    from kawasemi.ai_input.resource_index.schema import ResourceHit

    class _Store:
        def label_count(self, label):  # noqa: ANN001
            return {"架空区分コード": 250, "架空検体種別コード": 1}.get(label, 1)

    hits = [
        ResourceHit(cand_id=0, score=0.60, unit=_unit(Kind.DB, "db:A.1", conn="C", tbl="A")),
        ResourceHit(cand_id=1, score=0.60, unit=_unit(Kind.DB, "db:B.1", conn="C", tbl="B")),
    ]
    hits[0].unit.label = "架空区分コード"
    hits[1].unit.label = "架空検体種別コード"

    out = rd_mod._rerank_by_type(hits, EMPTY_HINT, 8, _Store())
    assert [h.unit.label for h in out] == ["架空検体種別コード", "架空区分コード"]
    assert out[0].score == 0.60, "cosine そのものは書き換えない（★の意味を変えない）"


def test_the_ranking_is_untouched_when_no_store_is_given():
    """`store` を渡さなければ定型列を下げない（施策抜きで縮退する）。"""
    from kawasemi.ai_input import resource_drafter as rd_mod
    from kawasemi.ai_input.location_hint import EMPTY_HINT
    from kawasemi.ai_input.resource_index.schema import ResourceHit

    hits = [
        ResourceHit(
            cand_id=i, score=0.60 - i * 0.001, unit=_unit(Kind.DB, f"db:X.{i}", conn="C", tbl="X")
        )
        for i in range(4)
    ]
    out = rd_mod._rerank_by_type(hits, EMPTY_HINT, 8)
    assert [h.unit.unit_id for h in out] == [h.unit.unit_id for h in hits]
