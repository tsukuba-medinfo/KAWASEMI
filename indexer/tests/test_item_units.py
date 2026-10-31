"""項目単位の生成と追記（`kawasemi_indexer.item_units`）。

施設の回答形式や表名に依らず、手書きの `ItemSpec` と決定的な embedder
（`HashingEmbedder`）だけで `build_index → append_item_units → ResourceIndexStore` が通る。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from test_resource_index import FIXTURE_DIR, HashingEmbedder

from kawasemi_indexer.builder import build_db_index
from kawasemi_indexer.item_units import (
    AppendResult,
    ItemSpec,
    append_item_units,
    build_item_units,
    numeric_share,
    read_db_units,
)
from kawasemi_indexer.store import ResourceIndexStore

# フィクスチャ（DEMO.LAB の WBC 列）に項目コードで行を選ぶ単位を足す。
SPECS = [
    ItemSpec(
        db_qualified="DEMO.LAB",
        value_column="WBC",
        item_code="TEMP01",
        item_name="体温",
        item_column="ITEM_CD",
        item_source="demo.ITEM_MASTER",
        row_filters={"IS_DELETED": "0", "IS_VALID": "1"},
        order_by=(("MEASURED_AT", "asc"),),
        measurements=["T", "体温"],
        units=["度"],
        patients=5000,
        numeric_share=numeric_share(95, 100),
    ),
    ItemSpec(
        db_qualified="DEMO.LAB",
        value_column="WBC",
        item_code="SPO2",
        item_name="SpO2",
        item_column="ITEM_CD",
        item_source="demo.ITEM_MASTER",
        row_filters={"IS_DELETED": "0"},
        order_by=(("MEASURED_AT", "asc"),),
        units=["%"],
        patients=1500,
    ),
]


@pytest.fixture
def base_index(tmp_path: Path) -> Path:
    out = tmp_path / "base"
    build_db_index(source=FIXTURE_DIR, out_dir=out, embedder=HashingEmbedder())
    return out


def test_build_item_units_copies_the_column_unit_and_adds_the_item(base_index: Path) -> None:
    units = {u["unit_id"]: u for u in build_item_units(SPECS, read_db_units(base_index))}
    assert set(units) == {"db:DEMO.LAB.WBC#TEMP01", "db:DEMO.LAB.WBC#SPO2"}
    temp = units["db:DEMO.LAB.WBC#TEMP01"]
    assert temp["kind"] == "db" and temp["label"] == "体温"
    # 検索文は項目の語が主。測定名のうち項目名と同じものは重ねない
    assert temp["search_text"] == "体温 T 度 検査結果"
    p = temp["payload"]
    assert (p["db_qualified"], p["physical_name"], p["logical_name"]) == (
        "DEMO.LAB",
        "WBC",
        "白血球数",
    ), "列の単位の payload を写していない"
    assert (p["item_code"], p["item_name"], p["item_filter_column"]) == (
        "TEMP01",
        "体温",
        "ITEM_CD",
    )
    assert p["item_row_filters"] == [
        {"column": "IS_DELETED", "equals": "0"},
        {"column": "IS_VALID", "equals": "1"},
    ]
    assert p["access_path"] == {
        "filters": [
            {"column": "ITEM_CD", "equals": "TEMP01"},
            {"column": "IS_DELETED", "equals": "0"},
            {"column": "IS_VALID", "equals": "1"},
        ],
        "order_by": [{"column": "MEASURED_AT", "dir": "asc"}],
    }
    assert (p["item_patients"], p["item_numeric_share"]) == (5000, 0.95)
    assert p["item_source"] == "demo.ITEM_MASTER"
    assert "ITEM_CD='TEMP01'" in p["notes"] and "IS_DELETED='0' AND IS_VALID='1'" in p["notes"]
    assert "絞り込み=DEMO.LAB.ITEM_CD='TEMP01' 患者数=5000" in temp["snippet"]


def test_item_spec_has_no_facility_defaults() -> None:
    """項目コード列と項目マスタは施設ごとに違うので既定値を持たない（呼び出し側が渡す）。"""
    with pytest.raises(TypeError):
        ItemSpec(db_qualified="DEMO.LAB", value_column="WBC", item_code="X", item_name="x")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ItemSpec(  # type: ignore[call-arg]
            db_qualified="DEMO.LAB",
            value_column="WBC",
            item_code="X",
            item_name="x",
            item_column="ITEM_CD",
        )


def test_build_item_units_refuses_a_column_that_is_not_in_the_index(base_index: Path) -> None:
    spec = ItemSpec(
        db_qualified="DEMO.LAB",
        value_column="NO_SUCH",
        item_code="X",
        item_name="x",
        item_column="ITEM_CD",
        item_source="demo.ITEM_MASTER",
    )
    with pytest.raises(KeyError, match="db:DEMO.LAB.NO_SUCH"):
        build_item_units([spec], read_db_units(base_index))


def test_notes_can_be_overridden_per_item(base_index: Path) -> None:
    spec = ItemSpec(
        db_qualified="DEMO.LAB",
        value_column="WBC",
        item_code="X",
        item_name="x",
        item_column="ITEM_CD",
        item_source="demo.ITEM_MASTER",
        notes="施設の注",
    )
    (unit,) = build_item_units([spec], read_db_units(base_index))
    assert unit["payload"]["notes"] == "施設の注"


def test_append_with_a_fake_embedder_makes_the_items_searchable(
    base_index: Path, tmp_path: Path
) -> None:
    """同じ fake embedder で作った索引へ追記し、store で読めて項目が検索で当たる。"""
    embedder = HashingEmbedder()
    old_vecs = np.load(base_index / "embeddings.npy")
    n_before = old_vecs.shape[0]
    units = build_item_units(SPECS, read_db_units(base_index))

    out = tmp_path / "out"
    result = append_item_units(base_index, out, units, embedder)
    assert isinstance(result, AppendResult)
    assert [u["unit_id"] for u in result.appended] == [u["unit_id"] for u in units]
    assert result.skipped == [] and result.n_db_before == n_before
    assert result.n_db_after == result.unit_count == n_before + 2

    store = ResourceIndexStore.load(out)
    assert store.is_loaded and store.is_searchable(embedder.model)
    assert store.unit_count == n_before + 2
    new_vecs = np.load(out / "embeddings.npy")
    assert new_vecs.shape == (store.unit_count, old_vecs.shape[1])
    assert (new_vecs[:n_before] == old_vecs).all(), "既存のベクトルが動いた"
    # 追記した行のベクトルは、その search_text を同じ embedder で埋めたものと一致する
    expected = embedder.embed([u["search_text"] for u in units])
    assert np.allclose(new_vecs[n_before:], expected)

    hits = store.search(embedder.embed(["体温"])[0], top_k=3)
    assert hits and hits[0].unit.unit_id == "db:DEMO.LAB.WBC#TEMP01"
    assert hits[0].unit.payload["item_code"] == "TEMP01"
    hits = store.search(embedder.embed(["SpO2"])[0], top_k=3)
    assert hits and hits[0].unit.unit_id == "db:DEMO.LAB.WBC#SPO2"

    info = json.loads((out / "build_info.json").read_text(encoding="utf-8"))
    base_info = json.loads((base_index / "build_info.json").read_text(encoding="utf-8"))
    assert info["kind_counts"]["db"] == base_info["kind_counts"]["db"] + 2
    assert info["unit_count"] == base_info["unit_count"] + 2
    assert (info["embedding_model"], info["dim"], info["schema_version"]) == (
        base_info["embedding_model"],
        base_info["dim"],
        base_info["schema_version"],
    ), "build_info の embedding の記録を動かした"


def test_append_skips_units_already_in_the_index_and_writes_nothing(
    base_index: Path, tmp_path: Path
) -> None:
    embedder = HashingEmbedder()
    units = build_item_units(SPECS, read_db_units(base_index))
    first = tmp_path / "first"
    append_item_units(base_index, first, units, embedder)

    again = tmp_path / "again"
    result = append_item_units(first, again, units, embedder)
    assert result.appended == [] and len(result.skipped) == 2
    assert not again.exists(), "追記するものが無いのに書いた"


def test_append_keeps_other_kinds_after_the_db_block(base_index: Path, tmp_path: Path) -> None:
    """db の後ろに別 kind の行があっても、挿し込み位置は db ブロックの末尾で、後ろの行は動かない。"""
    embedder = HashingEmbedder()
    units = build_item_units(SPECS[:1], read_db_units(base_index))
    # db 行の後ろに master 行を1つ足した索引を手で作る
    base_vecs = np.load(base_index / "embeddings.npy")
    extra = embedder.embed(["病名マスタ"])
    np.save(base_index / "embeddings.npy", np.vstack([base_vecs, extra]))
    (base_index / "master_index.jsonl").write_text(
        json.dumps(
            {
                "unit_id": "master:DEMO.DISEASE#1",
                "kind": "master",
                "label": "病名",
                "search_text": "病名マスタ",
                "snippet": "病名",
                "payload": {},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    info = json.loads((base_index / "build_info.json").read_text(encoding="utf-8"))
    info["kind_counts"]["master"] = 1
    info["unit_count"] += 1
    (base_index / "build_info.json").write_text(json.dumps(info), encoding="utf-8")

    out = tmp_path / "out"
    result = append_item_units(base_index, out, units, embedder)
    n_db = result.n_db_before
    merged = np.load(out / "embeddings.npy")
    assert merged.shape[0] == n_db + 1 + 1
    assert (merged[:n_db] == base_vecs).all() and (merged[-1] == extra[0]).all()
    assert (out / "master_index.jsonl").exists()
    store = ResourceIndexStore.load(out)
    assert store.is_loaded and store.unit_count == n_db + 2


def test_append_refuses_a_mismatched_embedder_dimension(base_index: Path, tmp_path: Path) -> None:
    units = build_item_units(SPECS[:1], read_db_units(base_index))
    with pytest.raises(ValueError, match="次元"):
        append_item_units(base_index, tmp_path / "out", units, HashingEmbedder(dim=8))
    assert not (tmp_path / "out").exists()


def test_append_refuses_a_base_whose_vector_rows_disagree_with_build_info(
    base_index: Path, tmp_path: Path
) -> None:
    """`embeddings.npy` の行数が `build_info.kind_counts` の合計と合わない索引へは挿し込まない。"""
    vecs = np.load(base_index / "embeddings.npy")
    np.save(base_index / "embeddings.npy", vecs[:-1])  # 1 行足りない
    units = build_item_units(SPECS[:1], read_db_units(base_index))
    with pytest.raises(ValueError, match="行数"):
        append_item_units(base_index, tmp_path / "out", units, HashingEmbedder())
    assert not (tmp_path / "out").exists()
