"""キー種別カタログ key_types.yaml ローダのユニットテスト。

- 未配置 / 壊れた yaml は空カタログへ縮退
- key_types セクション / 直接マッピングの両方を許容
- canonical の解釈（string / numeric_string / 未知kindはstringへ）
- label 既定（id）・system optional
- get / all / シングルトン get_catalog + reset_catalog_cache
"""

from __future__ import annotations

from pathlib import Path

from kawasemi.ai_input.resource_index.key_types import (
    CANONICAL_NUMERIC_STRING,
    CANONICAL_STRING,
    KeyTypeCatalog,
    get_catalog,
    reset_catalog_cache,
)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "key_types.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_file_is_empty(tmp_path: Path) -> None:
    cat = KeyTypeCatalog.load(tmp_path / "nope.yaml")
    assert cat.is_loaded is False
    assert cat.all() == []
    assert cat.get("mrn") is None


def test_broken_yaml_is_empty(tmp_path: Path) -> None:
    p = _write(tmp_path, "key_types: [unclosed\n")
    cat = KeyTypeCatalog.load(p)
    assert cat.is_loaded is False


def test_load_full_catalog(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
key_types:
  trial_subject_no:
    label: 登録番号
    canonical:
      kind: string
  mrn:
    label: 院内MRN
    system: "urn:oid:1.2.3"
    canonical:
      kind: numeric_string
      description: ゼロ詰め除去
""",
    )
    cat = KeyTypeCatalog.load(p)
    assert cat.is_loaded is True
    assert {k.id for k in cat.all()} == {"trial_subject_no", "mrn"}

    mrn = cat.get("mrn")
    assert mrn is not None
    assert mrn.label == "院内MRN"
    assert mrn.system == "urn:oid:1.2.3"
    assert mrn.canonical.kind == CANONICAL_NUMERIC_STRING
    assert mrn.canonical.description == "ゼロ詰め除去"

    subj = cat.get("trial_subject_no")
    assert subj is not None
    assert subj.system is None
    assert subj.canonical.kind == CANONICAL_STRING


def test_direct_mapping_without_wrapper(tmp_path: Path) -> None:
    p = _write(tmp_path, "mrn:\n  label: 院内MRN\n")
    cat = KeyTypeCatalog.load(p)
    assert cat.get("mrn") is not None


def test_defaults_label_to_id_and_unknown_kind_falls_back(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
key_types:
  bare: {}
  weird:
    canonical:
      kind: bogus
""",
    )
    cat = KeyTypeCatalog.load(p)
    bare = cat.get("bare")
    assert bare is not None and bare.label == "bare"  # label 既定 = id
    assert bare.canonical.kind == CANONICAL_STRING  # canonical 未指定 = string

    weird = cat.get("weird")
    assert weird is not None and weird.canonical.kind == CANONICAL_STRING  # 未知kind→string


def test_singleton_cache(tmp_path: Path) -> None:
    p = _write(tmp_path, "key_types:\n  mrn:\n    label: 院内MRN\n")
    reset_catalog_cache()
    a = get_catalog(str(p))
    b = get_catalog(str(p))
    assert a is b  # 同一パスはキャッシュ
    reset_catalog_cache()
    c = get_catalog(str(p))
    assert c is not a  # reset 後は再ロード
