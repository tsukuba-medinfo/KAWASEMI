"""列の充填状況による所在候補の足切り（`resource_index.column_fill`）のテスト。

このモジュールの主張は3つある。

1. **0件だけを落とす。** 充填率が低いことは「使えない」の証拠にならない。
   測ったのは表全体の充填率で、欲しいのは対象集団での充填率である。
   例えば `STAGE_T` / `STAGE_N` / `STAGE_M` が全体で15%しか埋まっていなくても、
   対象の症例では必ず入っているかもしれない。**閾値を置くとそういう列を落とす。**
2. **測っていないものは通す。** 測定の穴をそのまま候補の穴にしない。
3. **無ければ何もしない。** 成果物が未配置でも検索は動く（機能縮退）。
"""

from __future__ import annotations

import json
from pathlib import Path

from kawasemi.ai_input.resource_index.column_fill import ColumnFillIndex
from kawasemi.ai_input.resource_index.schema import Kind, ResourceUnit


def _unit(table: str = "SRC.ORDERS", column: str = "A", kind: str = Kind.DB) -> ResourceUnit:
    return ResourceUnit(
        unit_id=f"db:{table}.{column}",
        kind=kind,
        label=column,
        search_text=column,
        snippet="",
        payload={"connection_id": "dwh", "db_qualified": table, "physical_name": column},
    )


def _write(tmp_path: Path, *records: dict[str, object]) -> ColumnFillIndex:
    (tmp_path / "column_fill.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )
    return ColumnFillIndex.load(tmp_path)


def _rec(column: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "connection_id": "dwh",
        "db_qualified": "SRC.ORDERS",
        "physical_name": column,
        "exists": True,
        "total_count": 100,
        "filled_count": 100,
        "filled_rate": 100.0,
        "measured_at": "2000-01-01",
    }
    base.update(kw)
    return base


def test_a_column_with_no_value_is_dropped(tmp_path: Path) -> None:
    idx = _write(tmp_path, _rec("EMPTY", filled_count=0, filled_rate=0.0))
    assert not idx.is_acquirable(_unit(column="EMPTY"))


def test_a_column_absent_from_the_database_is_dropped(tmp_path: Path) -> None:
    """在るが空と、そもそも無いを区別する。**どちらも落とすが理由が違う。**"""
    idx = _write(
        tmp_path, _rec("GONE", exists=False, total_count=None, filled_count=None, filled_rate=None)
    )
    assert not idx.is_acquirable(_unit(column="GONE"))
    fill = idx.of(_unit(column="GONE"))
    assert fill is not None and fill.exists is False


def test_a_sparsely_filled_column_is_kept(tmp_path: Path) -> None:
    """`STAGE_T` が全体で15%しか埋まっていなくても落とさない。**閾値を置かない。**"""
    idx = _write(tmp_path, _rec("STAGE_T", total_count=1000, filled_count=150, filled_rate=15.0))
    assert idx.is_acquirable(_unit(column="STAGE_T"))


def test_a_column_filled_once_is_kept(tmp_path: Path) -> None:
    """1件でも入っていれば落とさない。0件だけが部分集合へ伝播する。"""
    idx = _write(tmp_path, _rec("RARE", total_count=1000, filled_count=1, filled_rate=0.1))
    assert idx.is_acquirable(_unit(column="RARE"))


def test_an_unmeasured_column_is_kept(tmp_path: Path) -> None:
    """測定の穴をそのまま候補の穴にしない。"""
    idx = _write(tmp_path, _rec("A"))
    assert idx.is_acquirable(_unit(column="NOT_MEASURED"))


def test_non_db_units_are_never_dropped(tmp_path: Path) -> None:
    """文書・規約のユニットは列ではない。充填の判定を当てない。"""
    idx = _write(tmp_path, _rec("A", filled_count=0))
    assert idx.of(_unit(column="A", kind=Kind.FILE)) is None
    assert idx.is_acquirable(_unit(column="A", kind=Kind.FILE))


def test_a_missing_artifact_disables_the_filter(tmp_path: Path) -> None:
    idx = ColumnFillIndex.load(tmp_path)
    assert not idx.is_loaded
    assert idx.is_acquirable(_unit(column="EMPTY"))


def test_a_broken_artifact_disables_the_filter(tmp_path: Path) -> None:
    """壊れた成果物で検索が全滅しない（索引の異常はすべて縮退で扱う設計）。"""
    (tmp_path / "column_fill.jsonl").write_text("{壊れている", encoding="utf-8")
    idx = ColumnFillIndex.load(tmp_path)
    assert not idx.is_loaded
    assert idx.is_acquirable(_unit(column="EMPTY"))


# --------------------------------------------------------------------------- #
# 文書種別ユニットの到達可能性（`unit_id` 鍵の行）
#
# 所在の特定と到達可能性は別の問題である。**落とすのは、ヘッダが1件も無い種別だけ**。
# ヘッダがあるのに本文が無い種別（`PATH`）は、所在は特定できるが現在の取得経路では引けない
# という所見なので、落とさない。
# --------------------------------------------------------------------------- #


def _doctype_unit(code: str) -> ResourceUnit:
    return ResourceUnit(
        unit_id=f"db:SRC.DOCUMENT.NOTE_TEXT#{code}",
        kind=Kind.DB,
        label=code,
        search_text=code,
        snippet="",
        payload={
            "connection_id": "ehr",
            "db_qualified": "SRC.DOCUMENT",
            "physical_name": "NOTE_TEXT",
            "doc_type_code": code,
        },
    )


def _reach(code: str, *, header: bool, body: bool) -> dict[str, object]:
    return {
        "unit_id": f"db:SRC.DOCUMENT.NOTE_TEXT#{code}",
        "has_header": header,
        "has_body": body,
        "header_count": 120 if header else 0,
        "body_count": 120 if body else 0,
        "measured_at": "2000-01-01",
    }


def test_a_doctype_without_headers_is_not_offered(tmp_path: Path) -> None:
    """複製された文書が1件も無い種別（`UNUSED` / `PATH_IMG`）は候補から外す。"""
    fill = _write(tmp_path, _reach("UNUSED", header=False, body=False))
    assert fill.is_acquirable(_doctype_unit("UNUSED")) is False


def test_a_doctype_with_headers_but_no_body_is_still_offered(tmp_path: Path) -> None:
    """ヘッダがあれば、本文が無くても**落とさない**（`PATH`。所在は特定できるが引けない）。"""
    fill = _write(tmp_path, _reach("PATH", header=True, body=False))
    unit = _doctype_unit("PATH")
    assert fill.is_acquirable(unit) is True
    reach = fill.reach_of(unit)
    assert reach is not None and reach.body_unreachable is True, "所見として区別できること"


def test_a_reachable_or_unmeasured_doctype_is_offered(tmp_path: Path) -> None:
    fill = _write(tmp_path, _reach("NOTE", header=True, body=True))
    assert fill.is_acquirable(_doctype_unit("NOTE")) is True
    assert fill.reach_of(_doctype_unit("NOTE")).body_unreachable is False  # type: ignore[union-attr]
    # **測っていない種別は通す**（測定の穴をそのまま候補の穴にしない）
    assert fill.is_acquirable(_doctype_unit("ZZ99")) is True
    assert fill.reach_of(_doctype_unit("ZZ99")) is None


def test_reach_rows_and_column_rows_live_in_one_file_without_interfering(tmp_path: Path) -> None:
    """列の行と文書種別の行（`unit_id` 鍵）が同じファイルに在っても互いに干渉しない。"""
    fill = _write(
        tmp_path,
        _rec("EMPTY", filled_count=0, filled_rate=0.0),
        _reach("UNUSED", header=False, body=False),
    )
    assert len(fill) == 2 and fill.is_loaded
    assert fill.is_acquirable(_unit(column="EMPTY")) is False  # 従来どおり0件の列は落とす
    assert fill.is_acquirable(_unit(column="OTHER")) is True
    assert fill.is_acquirable(_doctype_unit("UNUSED")) is False


def test_the_doctype_unit_is_decided_by_reach_not_by_the_column_it_shares(tmp_path: Path) -> None:
    """文書種別ユニットは `NOTE_TEXT` 列を共有する。**列が空でも種別の判定は種別で決める**。

    `(ehr, SRC.DOCUMENT, NOTE_TEXT)` の列の行が「0件」でも、種別ごとの到達可能性が
    あるユニットは、そちらで決まる（列の行は表全体の充填状況で、種別を区別できない）。
    """
    fill = _write(
        tmp_path,
        {
            "connection_id": "ehr",
            "db_qualified": "SRC.DOCUMENT",
            "physical_name": "NOTE_TEXT",
            "exists": True,
            "total_count": 100,
            "filled_count": 0,
            "filled_rate": 0.0,
            "measured_at": "2000-01-01",
        },
        _reach("NOTE", header=True, body=True),
    )
    assert fill.is_acquirable(_doctype_unit("NOTE")) is True


def test_an_unmeasured_doctype_is_not_dragged_down_by_the_shared_empty_column(
    tmp_path: Path,
) -> None:
    """**測っていない種別ユニットは、共有する列の0件の行に引きずられて落ちない**。

    種別ユニットは `NOTE_TEXT` 列（鍵が (connection_id, db_qualified, physical_name)）を
    共有する。列の行が0件でも、到達可能性が未測定の種別は「測っていないものは通す」。
    以前は `of(unit)` の列の行へ落ちて、未測定の種別が落ちた。
    """
    fill = _write(
        tmp_path,
        {
            "connection_id": "ehr",
            "db_qualified": "SRC.DOCUMENT",
            "physical_name": "NOTE_TEXT",
            "exists": True,
            "total_count": 100,
            "filled_count": 0,
            "filled_rate": 0.0,
            "measured_at": "2000-01-01",
        },
        _reach("NOTE", header=True, body=True),
    )
    assert fill.is_acquirable(_doctype_unit("ZZ99")) is True, "未測定の種別が列の0件で落ちた"
    # 種別でない通常の列は、従来どおり列の行で決まる（0件なら落とす）
    plain = ResourceUnit(
        unit_id="db:SRC.DOCUMENT.NOTE_TEXT",
        kind=Kind.DB,
        label="NOTE_TEXT",
        search_text="NOTE_TEXT",
        snippet="",
        payload={
            "connection_id": "ehr",
            "db_qualified": "SRC.DOCUMENT",
            "physical_name": "NOTE_TEXT",
        },
    )
    assert fill.is_acquirable(plain) is False
