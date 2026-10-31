"""集計の対象項目（`target_items.py`）のテスト。

分母は様式ごとの設定（`target_items.yaml`）で決まり、設定が無ければ汎用の既定で決まる。
値はすべて架空のもの。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kawasemi.ai_input.target_items import (
    EXCLUDED,
    EXTENSION,
    TARGET,
    TargetItemsError,
    item_number,
    load_target_items,
    resolve_targets,
)
from kawasemi.core.config import Settings, settings

_URL = "http://kawasemi.example/Questionnaire/targets"

_FIELDS: list[dict[str, Any]] = [
    {"link_id": "a", "text": "[1] 登録番号"},
    {"link_id": "b", "text": "[2] 生年月日"},
    {"link_id": "c", "text": "[3] 性別"},
    {"link_id": "d", "text": "[9] 同意取得日"},
    {"link_id": "e", "text": "補足（番号なし）"},
]


def test_item_number_reads_the_leading_bracket() -> None:
    assert item_number("[12] 体重") == "12"
    assert item_number("体重 [12]") is None
    assert item_number(None) is None


def test_default_path_is_under_site_dir() -> None:
    assert Settings(site_dir="/x/site").target_items_file == "/x/site/target_items.yaml"


def test_without_config_every_numbered_item_is_a_target() -> None:
    """設定の無い様式は、番号を持つ項目すべてが対象（番号なしは extension）。"""
    t = resolve_targets(_URL, _FIELDS)
    assert t.link_ids == ["a", "b", "c", "d"]
    assert t.expected == 4, "期待件数は既定で見つかった対象の数"
    assert t.missing_numbers == []
    assert t.kinds["e"] == EXTENSION


def test_without_config_and_without_numbers_every_item_is_a_target() -> None:
    """番号を持つ項目が1つも無い様式は、回答できる項目すべてが対象。"""
    fields = [{"link_id": "x", "text": "身長"}, {"link_id": "y", "text": "体重"}]
    t = resolve_targets(_URL, fields)
    assert t.link_ids == ["x", "y"]
    assert t.expected == 2
    assert set(t.kinds.values()) == {TARGET}


def test_config_selects_exactly_the_configured_numbers(target_items_config) -> None:
    """設定のある様式は、設定の番号だけが対象で、期待件数は設定の件数。"""
    target_items_config(
        {_URL: {"target_item_numbers": ["1", "3", "4"], "excluded_item_numbers": ["9"]}}
    )
    t = resolve_targets(_URL, _FIELDS)
    assert t.link_ids == ["a", "c"]
    assert t.expected == 3
    assert t.missing_numbers == ["4"], "設定にあるのに問診票に無い番号は数えて返す"
    assert t.kinds == {"a": TARGET, "b": EXCLUDED, "c": TARGET, "d": EXCLUDED, "e": EXTENSION}


def test_config_for_another_form_does_not_apply(target_items_config) -> None:
    """設定は url の完全一致で引く。載っていない様式は既定。"""
    target_items_config({_URL + "/other": {"target_item_numbers": ["1"]}})
    assert resolve_targets(_URL, _FIELDS).expected == 4
    assert resolve_targets(None, _FIELDS).expected == 4


def test_missing_file_is_empty_config(tmp_path: Path) -> None:
    assert load_target_items(str(tmp_path / "none.yaml")) == {}


def test_integer_numbers_are_accepted(tmp_path: Path) -> None:
    p = tmp_path / "t.yaml"
    p.write_text(f'questionnaires:\n  "{_URL}":\n    target_item_numbers: [1, 2]\n')
    assert load_target_items(str(p))[_URL].target_numbers == frozenset({"1", "2"})


@pytest.mark.parametrize(
    "body",
    [
        "questionnaires: [",  # YAML として読めない
        "- a\n- b\n",  # 対応表でない
        "questionnaires: [a]\n",  # questionnaires が対応表でない
        f'questionnaires:\n  "{_URL}": {{}}\n',  # target_item_numbers が無い
        f'questionnaires:\n  "{_URL}":\n    target_item_numbers: "1 2"\n',  # 文字列
        f'questionnaires:\n  "{_URL}":\n    target_item_numbers: ["A1"]\n',  # 数字でない
        f'questionnaires:\n  "{_URL}":\n    target_item_numbers: ["1", "1"]\n',  # 重複
        f'questionnaires:\n  "{_URL}":\n    target_item_numbers: ["1"]\n'
        '    excluded_item_numbers: ["1"]\n',  # 対象と対象外の両方
        f'questionnaires:\n  "{_URL}":\n    target_item_numbers: ["1"]\n    extra: []\n',
    ],
)
def test_malformed_config_is_an_error(tmp_path: Path, body: str) -> None:
    """形が正しくなければ例外にする（黙って既定に落とすと分母の変化に気づけない）。"""
    p = tmp_path / "t.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(TargetItemsError):
        load_target_items(str(p))


def test_malformed_config_fails_the_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    p = tmp_path / "t.yaml"
    p.write_text("questionnaires: [a]\n", encoding="utf-8")
    monkeypatch.setattr(settings, "target_items_file", str(p))
    with pytest.raises(TargetItemsError):
        resolve_targets(_URL, _FIELDS)


def test_site_example_matches_the_e2e_questionnaire() -> None:
    """雛形（`site.example/target_items.yaml`）が E2E の様式の番号を指していること。"""
    root = Path(__file__).resolve().parents[2]
    table = load_target_items(str(root / "site.example/target_items.yaml"))
    questionnaire = json.loads(
        (root / "examples/e2e/questionnaire_e2e_infection.json").read_text(encoding="utf-8")
    )
    spec = table[questionnaire["url"]]
    numbers = {item_number(i.get("text")) for i in questionnaire["item"]}
    assert spec.target_numbers | spec.excluded_numbers <= numbers
