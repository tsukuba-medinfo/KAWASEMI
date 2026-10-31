"""臨床的意味 Extension の複合化のテスト。

`clinical-annotation` Extension は `valueString` 単一値で、記述しか運べなかった。配布された
Questionnaire を受け取った施設は**記述の根拠を辿れない**。複合 Extension にして典拠を同行させる。

ここで固定するのは次の4点:

1. **配布して良いものだけが出る。** 典拠の `payload` は型検査を通っていない開いた dict で、
   キーを供給するのは索引側（`indexer/`）である。許可リスト方式であること、特に規約文書の本文
   （`snippet`）と施設のインフラ識別子（`connection_id`）が**絶対に出ない**こと。
2. **往復で情報が落ちない。** 書き出した施設が読み戻したとき、配布に載らない手元の値
   （snippet 等）が消えないこと。受け取った施設には配布された部分集合が入ること。
3. **旧形式（`valueString` 単一値）を読めること**（後方互換）。
4. **StructureDefinition と実装がずれないこと。** 下位要素名の正本は SD で、コードの定数と
   一致していることを検査する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import AnnotationStatus, ClinicalAnnotation
from kawasemi.ai_input.routes import _citation_keys, _merge_imported_citations
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from kawasemi.utils.file_export import (
    _CITATION_PART_BUILDERS,
    CLINICAL_ANNOTATION_EXTENSION_URL,
    EXPORTABLE_CITATION_PAYLOAD_KEYS,
    ORIGIN_IMPORTED,
    SUB_ANNOTATION,
    SUB_CITATION,
    SUB_CITATION_CHAR_RANGE,
    SUB_CITATION_LLM_MODEL,
    SUB_CITATION_MIME,
    SUB_CITATION_PAGE,
    SUB_CITATION_SOURCE,
    SUB_CITATION_UNIT_ID,
    embed_clinical_annotations,
    extract_clinical_annotations,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SD_PATH = _REPO_ROOT / "fhir/structuredefinitions/kawasemi-clinical-annotation.json"

# 索引（`indexer/`）が作る reference ユニットの payload に、UI が snippet / label を
# 足した形。**実運用で保存される最大構成**なので、配布の検査はこれを起点にする。
_FULL_PAYLOAD: dict[str, Any] = {
    "connection_id": "conn-local-files",
    "file_role": "reference",
    "path": "manuals/登録の手引き.pdf",
    "page": 12,
    "char_range": [340, 780],
    "mime": "application/pdf",
    "snippet": "診断日は病理診断の確定日を採る。臨床診断のみの場合は…",
    "label": "規約文書: manuals/登録の手引き.pdf p.12（340-780）",
}
_UNIT_ID = "file:manuals/登録の手引き.pdf#p12:340-780"
_CITATION: dict[str, Any] = {
    "unit_id": _UNIT_ID,
    "payload": dict(_FULL_PAYLOAD),
    "llm_model": "qwen3:8b",
}


# 索引に無い紙資料を人が典拠として書いたもの（UI の自由入力）。**配布物では表現できない**が
# 保存はできる（`_clean_citation` は unit_id 無しでも payload があれば通す）。
_HANDWRITTEN: dict[str, Any] = {
    "unit_id": None,
    "payload": {"label": "紙の院内マニュアル 第3版 p.7", "snippet": "診断日の定義…"},
    "llm_model": None,
}


def _content(link_ids: tuple[str, ...] = ("q1",)) -> dict[str, Any]:
    return {
        "resourceType": "Questionnaire",
        "id": "q-ext",
        "item": [{"linkId": lid, "text": f"項目{lid}", "type": "string"} for lid in link_ids],
    }


def _extension_of(content: dict[str, Any], link_id: str = "q1") -> dict[str, Any]:
    item = next(i for i in content["item"] if i["linkId"] == link_id)
    return next(e for e in item["extension"] if e["url"] == CLINICAL_ANNOTATION_EXTENSION_URL)


def _sub(ext: dict[str, Any], url: str) -> list[dict[str, Any]]:
    return [e for e in ext.get("extension", []) if e.get("url") == url]


# =============================================================================
# 1. 配布して良いものだけが出る
# =============================================================================


def test_annotation_and_citation_travel_together() -> None:
    """記述と典拠が同じ Extension の下位要素として載ること（複合化の本体）。"""
    out, _ = embed_clinical_annotations(_content(), {"q1": "診断確定日"}, {"q1": [_CITATION]})
    ext = _extension_of(out)
    assert "valueString" not in ext, "複合 Extension は値を持たない"
    assert [e["valueString"] for e in _sub(ext, SUB_ANNOTATION)] == ["診断確定日"]
    citations = _sub(ext, SUB_CITATION)
    assert len(citations) == 1
    parts = {e["url"]: e for e in citations[0]["extension"]}
    assert parts[SUB_CITATION_UNIT_ID]["valueString"] == _UNIT_ID
    assert parts[SUB_CITATION_PAGE]["valueInteger"] == 12
    assert parts[SUB_CITATION_CHAR_RANGE]["valueString"] == "340-780"
    assert parts[SUB_CITATION_MIME]["valueString"] == "application/pdf"
    assert parts[SUB_CITATION_LLM_MODEL]["valueString"] == "qwen3:8b"


@pytest.mark.parametrize("secret", ["snippet", "connection_id"])
def test_the_forbidden_payload_keys_never_reach_the_extension(secret: str) -> None:
    """規約文書の本文と施設のインフラ識別子が、**値としてもキーとしても**出ないこと。

    payload を丸ごと載せる実装への退行を落とす。文字列で全文検査するのは、下位要素の
    どこか（想定外の階層）に紛れ込む形も捕まえたいため。
    """
    out, _ = embed_clinical_annotations(_content(), {"q1": "診断確定日"}, {"q1": [_CITATION]})
    dumped = json.dumps(out, ensure_ascii=False)
    assert secret not in dumped
    assert str(_FULL_PAYLOAD[secret]) not in dumped


def test_only_allowlisted_payload_keys_are_exported() -> None:
    """許可リストに無いキーは、**索引が新しく足したものでも**出ないこと。

    payload のキーを供給するのは索引側（`indexer/`）で、こちらの許可リストを
    参照しない。拒否リスト方式だと、向こうがキーを足した瞬間にそれが施設外へ出る。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {**_FULL_PAYLOAD, "patient_key": "MRN", "notes": "施設の定義書メモ"},
        "llm_model": None,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "診断確定日"}, {"q1": [citation]})
    dumped = json.dumps(out, ensure_ascii=False)
    assert "patient_key" not in dumped and "MRN" not in dumped
    assert "notes" not in dumped and "施設の定義書メモ" not in dumped


@pytest.mark.parametrize(
    "unit_id",
    [
        "患者 山田太郎 (MRN 0000123456) のカルテ 2024-05-02 より",  # 人が自由に書いた
        "紙の院内マニュアル 第3版 p.7",
        "file:manuals/手引き.pdf",  # 位置が無い
        "db:dwh.patient_infomation",  # 規約文書ではない
        "",
    ],
)
def test_a_citation_whose_unit_id_is_not_an_index_address_is_not_distributed(
    unit_id: str,
) -> None:
    """`unit_id` が索引のアドレス形式でない典拠を配布しないこと。

    許可リストが守るのは payload の**キー**だけで、`unit_id` の**値**は守らない。そして
    `unit_id` は UI の自由入力欄なので、人が患者名を書ける。`snippet` を外した理由
    （人が任意の文字列を書ける）は、そのまま `unit_id` にも当てはまる。
    """
    citation = {
        "unit_id": unit_id,
        "payload": {"file_role": "reference", "path": "manuals/手引き.pdf"},
        "llm_model": None,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    dumped = json.dumps(out, ensure_ascii=False)
    assert SUB_CITATION not in dumped
    if unit_id:
        assert unit_id not in dumped


def test_a_patient_data_citation_is_not_distributed() -> None:
    """`file_role` が reference でない典拠（患者データ由来）を配布しないこと。

    AI 経路は reference に絞るが、保存ルートは絞らないので patient_data の典拠は保存できる。
    それが施設外へ出るのは二重に誤り（そもそも臨床的意味に混ざること自体が異常）。
    """
    citation = {
        "unit_id": "file:patient_data/病理レポート.txt#p1:0-100",
        "payload": {
            "file_role": "patient_data",
            "path": "patient_data/病理レポート.txt",
            "mime": "text/plain",
        },
        "llm_model": None,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    dumped = json.dumps(out, ensure_ascii=False)
    assert SUB_CITATION not in dumped
    assert "病理レポート" not in dumped


def test_the_import_does_not_fabricate_the_file_role() -> None:
    """取り込みが `file_role` を補わないこと（由来の捏造をしない）。

    配布物に無いものを `reference` と書けば、下流の検査（規約文書かどうかの判定）を
    すり抜ける形を自分で作ることになる。
    """
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [_CITATION]})
    extracted = extract_clinical_annotations(out)
    assert "file_role" not in (extracted.citations_for("q1")[0]["payload"] or {})


@pytest.mark.parametrize(
    "char_range",
    [[1, 2, 3], ["a", "b"], [5], [], [True, False], "340-780"],
)
def test_a_malformed_char_range_is_not_distributed(char_range: Any) -> None:
    """`[start, end]`（整数2個）以外の `char_range` を載せないこと。

    読み込み側は `start-end` しか戻せないので、書き出しだけ緩いと往復で消える
    （`payload` はクライアント由来なので、この形は普通に作れる）。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {
            "file_role": "reference",
            "path": "manuals/手引き.pdf",
            "char_range": char_range,
        },
        "llm_model": None,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    ext = _extension_of(out)
    assert _sub(_sub(ext, SUB_CITATION)[0], SUB_CITATION_CHAR_RANGE) == []
    # 往復しても消えない（＝載っていないものを復元しようとしない）
    extracted = extract_clinical_annotations(out)
    assert "char_range" not in (extracted.citations_for("q1")[0]["payload"] or {})


_PHI = "患者 山田太郎（ID 0000001234）病理で腺癌 主治医メモ"


@pytest.mark.parametrize(
    "payload_extra,llm_model",
    [
        ({"page": _PHI}, None),  # ページ番号のふりをした自由文
        ({"char_range": {"snippet": _PHI}}, None),  # 位置のふりをした dict
        ({"char_range": [_PHI, _PHI]}, None),
        # **空白を含まない**漢字混じり。Python の `\w` は仮名・漢字を含むので、文字クラスを
        # `[\w.+-]` に緩めるとこれが通る（空白入りの例では緩めても弾かれ、退行が見えない）。
        ({"mime": "application/pdf患者山田太郎ID0000001234病理で腺癌"}, None),
        ({"mime": f"application/pdf {_PHI}"}, None),
        ({"page": 12}, f"qwen3:8b {_PHI}"),  # モデル名のふりをした自由文
        ({}, _PHI),
    ],
)
def test_no_unchecked_value_reaches_the_extension(
    payload_extra: dict[str, Any], llm_model: str | None
) -> None:
    """**許可キーのどの値も、検査を通らずに配布物へ出ない**こと。

    許可リストが守るのはキーで、値は別。しかも値の経路は下位要素だけではない——出典表記
    （`source`）も同じ値を読む。**部品を作る経路と表記を作る経路が2本ある**と、片方だけ
    検査して安心する（実際に2周続けて起きた）。ここは配布物の**全文**を見る。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {"file_role": "reference", **payload_extra},
        "llm_model": llm_model,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    assert "山田太郎" not in json.dumps(out, ensure_ascii=False)


def test_the_source_text_is_built_from_the_emitted_parts() -> None:
    """出典表記が、**実際に下位要素として載った値**だけから組み立てられていること。

    検査に落ちた `page` が `source` には出る、という非対称を落とす。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {"file_role": "reference", "page": -3, "char_range": [780, 340]},
        "llm_model": None,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    citation_ext = _sub(_extension_of(out), SUB_CITATION)[0]
    assert _sub(citation_ext, SUB_CITATION_PAGE) == []
    assert _sub(citation_ext, SUB_CITATION_CHAR_RANGE) == []
    source = _sub(citation_ext, SUB_CITATION_SOURCE)[0]["valueString"]
    assert source == "規約文書: manuals/登録の手引き.pdf"


def test_page_zero_still_appears_in_the_source() -> None:
    """`page=0` が出典表記から落ちないこと（`if page` と書くと消える）。"""
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {"file_role": "reference", "page": 0},
        "llm_model": None,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    source = _sub(_sub(_extension_of(out), SUB_CITATION)[0], SUB_CITATION_SOURCE)[0]["valueString"]
    assert "p.0" in source


@pytest.mark.parametrize("page", [True, False, -1, "12", 12.0, None])
def test_a_malformed_page_is_not_distributed(page: Any) -> None:
    """`page` の値が非負の整数でなければ載せないこと（bool は int の派生なので明示的に弾く）。"""
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {"file_role": "reference", "page": page},
        "llm_model": None,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    assert _sub(_sub(_extension_of(out), SUB_CITATION)[0], SUB_CITATION_PAGE) == []


@pytest.mark.parametrize(
    "llm_model",
    ["qwen3:8b のカルテ本文", "患者データ", "a" * 100, "モデル", " qwen3:8b\n田中"],
)
def test_a_malformed_llm_model_is_not_distributed(llm_model: str) -> None:
    """`llm_model` の値がモデル名の形でなければ載せないこと。

    「モデル名だけで施設情報を含まない」と主張するなら、それを強制する。`_clean_citation` は
    strip しかしないので、保存 API から任意の文字列を書ける。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {"file_role": "reference"},
        "llm_model": llm_model,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    assert _sub(_sub(_extension_of(out), SUB_CITATION)[0], SUB_CITATION_LLM_MODEL) == []


@pytest.mark.parametrize(
    "unit_id",
    [
        "file:患者 山田太郎 のカルテ:0-0",  # 空白を含む自由文
        "file:患者\u3000山田太郎:0-0",  # 全角空白
        "file:" + "a" * 250 + ":0-0",  # 極端に長い
        "file:改行\nあり:0-0",
    ],
)
def test_a_unit_id_with_free_text_shaped_path_is_not_distributed(unit_id: str) -> None:
    """`unit_id` のパス部分に空白・制御文字・過長を許さないこと。

    **これは形の検査であって、索引への実在確認ではない**（空白を含まない任意の文字列は
    通りうる）。実在確認は保存時の索引照合が担う（`test_citation_unit_verification.py`）。ここで狭めているのは自由文が紛れ込む面である。
    """
    citation = {
        "unit_id": unit_id,
        "payload": {"file_role": "reference"},
        "llm_model": None,
    }
    _, embedded = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    assert embedded == 0


def test_link_ids_with_citations_includes_items_with_zero_citations() -> None:
    """典拠0件の項目も「典拠を運んできた」に含めること。

    ここが落ちると、書き出し直しで「0件だと言っている」情報が「運んでいない」に化ける。
    """
    out, _ = embed_clinical_annotations(
        _content(("q1", "q2")), {"q1": "記述", "q2": "記述2"}, {"q1": [_CITATION]}
    )
    extracted = extract_clinical_annotations(out)
    assert extracted.link_ids_with_citations() == frozenset({"q1", "q2"})


def test_the_source_text_ignores_the_stored_path() -> None:
    """出典表記のパスが**検証済みの `unit_id` 由来**であること（`payload["path"]` を見ない）。

    `payload` の**値**は誰も検査していない（`_clean_citation` は dict かどうかだけ）。
    `payload["path"]` を優先すると、`unit_id` に入れた形式検査を丸ごと迂回して任意の
    文字列を配布物へ載せられる。許可キーであることと、値が安全であることは別。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {
            "file_role": "reference",
            "path": "患者 山田太郎（ID 0000001234）主治医メモ 転移あり",
            "page": 12,
        },
        "llm_model": None,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    dumped = json.dumps(out, ensure_ascii=False)
    assert "山田太郎" not in dumped
    source = _sub(_sub(_extension_of(out), SUB_CITATION)[0], SUB_CITATION_SOURCE)[0]["valueString"]
    assert source == "規約文書: manuals/登録の手引き.pdf p.12"


def test_the_round_trip_is_stable_even_if_the_stored_path_disagrees() -> None:
    """`payload["path"]` が `unit_id` と食い違っていても往復が冪等であること。

    書き出しが payload を、読み込みが `unit_id` を見ていると、往復のたびに `source` が
    変わる（受け入れ条件「読み戻して書き出し直しても同じ Extension」の反例になる）。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {"file_role": "reference", "path": "別のパス.pdf", "page": 12},
        "llm_model": None,
    }
    first, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    extracted = extract_clinical_annotations(first)
    second, _ = embed_clinical_annotations(
        _content(), extracted.annotations, {"q1": extracted.citations_for("q1") or []}
    )
    assert second == first


@pytest.mark.parametrize(
    "mime",
    [
        "患者データ 生年月日 1950-01-01",
        "application",
        "/pdf",
        "  ",
        "text/plain; 山田",
        # 長さ上限（`llm_model` 側と対称にする。意図して入れた上限は無検査にしない）
        "application/" + "a" * 100,
        "a" * 100 + "/pdf",
    ],
)
def test_a_malformed_mime_is_not_distributed(mime: str) -> None:
    """`mime` の**値**が MIME タイプの形でなければ載せないこと。

    ここも許可キーだが、値は保存 API から任意に書ける。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {"file_role": "reference", "mime": mime},
        "llm_model": None,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    ext = _extension_of(out)
    assert _sub(_sub(ext, SUB_CITATION)[0], SUB_CITATION_MIME) == []
    if mime.strip():
        assert mime.strip() not in json.dumps(out, ensure_ascii=False)


def test_a_citation_without_a_known_origin_is_not_distributed() -> None:
    """`file_role` を持たない典拠を配布しないこと（**フェイルクローズ**）。

    「reference でなければ拒む」ではなく「reference だと分かるものだけ通す」。前者だと
    索引が新しいロールを付けたときや、単に付け忘れたときに配布されてしまう。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {"page": 12},  # file_role も origin も無い
        "llm_model": None,
    }
    out, embedded = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    assert embedded == 0
    assert SUB_CITATION not in json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize(
    "role",
    [
        "master",
        "patient_data",
        "unknown-new-role",
        "",
        # 前方一致で通してしまう形（`startswith("reference")` への退行を落とす）
        "reference_patient_data",
        "reference-copy",
    ],
)
def test_only_reference_citations_are_distributed(role: str) -> None:
    """reference 以外のロールは、既知・未知を問わず配布しないこと。"""
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {"file_role": role, "page": 12},
        "llm_model": None,
    }
    _, embedded = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    assert embedded == 0


@pytest.mark.parametrize("origin", ["手入力", "imported", "", "imported-extension-ish"])
def test_only_the_exact_origin_marker_allows_redistribution(origin: str) -> None:
    """`origin` は**値が一致**したときだけ再配布の根拠になること。

    キーの存在だけで通すと、`payload` に `origin` を書くだけでフェイルクローズを迂回できる
    （`payload` は保存 API から任意に書ける）。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {"origin": origin, "page": 12},  # file_role は無い
        "llm_model": None,
    }
    _, embedded = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [citation]})
    assert embedded == 0


def test_an_imported_citation_can_be_redistributed() -> None:
    """配布物から取り込んだ典拠を、受け取った施設が再配布できること。

    取り込みは `file_role` を復元しない（捏造しない）ので、フェイルクローズを素朴に
    書くと**受け取った施設が再配布できなくなる**。取り込み時に記録する来歴（`origin`）を
    第2の根拠として認めることで、捏造せずに再配布を通す。
    """
    out, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [_CITATION]})
    extracted = extract_clinical_annotations(out)
    imported = extracted.citations_for("q1") or []
    assert imported and "file_role" not in (imported[0]["payload"] or {})
    assert (imported[0]["payload"] or {})["origin"] == ORIGIN_IMPORTED

    again, embedded = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": imported})
    assert embedded == 1
    assert again == out, "再配布で内容が変わってはいけない"


def test_the_source_text_is_rebuilt_not_copied() -> None:
    """出典表記は保存値（`payload["label"]`）ではなく組み立て直した値であること。

    `label` はクライアントから送られた値で、`_clean_citation` は中身を検査しない。配布物に
    そのまま載せると、検証されていない文字列が典拠の顔になる。
    """
    citation = {
        "unit_id": _UNIT_ID,
        "payload": {**_FULL_PAYLOAD, "label": "うその出典（人が書き換えた）"},
        "llm_model": None,
    }
    out, _ = embed_clinical_annotations(_content(), {"q1": "診断確定日"}, {"q1": [citation]})
    ext = _extension_of(out)
    source = _sub(_sub(ext, SUB_CITATION)[0], SUB_CITATION_SOURCE)[0]["valueString"]
    assert source == "規約文書: manuals/登録の手引き.pdf p.12（340-780）"
    assert "うその出典" not in json.dumps(out, ensure_ascii=False)


def test_embedding_is_idempotent() -> None:
    """2回埋め込んでも Extension が増えないこと（既存の置き換え）。"""
    once, _ = embed_clinical_annotations(_content(), {"q1": "診断確定日"}, {"q1": [_CITATION]})
    twice, _ = embed_clinical_annotations(once, {"q1": "診断確定日"}, {"q1": [_CITATION]})
    item = twice["item"][0]
    assert len([e for e in item["extension"] if e["url"] == CLINICAL_ANNOTATION_EXTENSION_URL]) == 1
    assert twice == once


def test_other_extensions_are_left_alone() -> None:
    """他人の Extension を巻き込まないこと。"""
    content = _content()
    content["item"][0]["extension"] = [{"url": "http://example.org/other", "valueString": "x"}]
    out, _ = embed_clinical_annotations(content, {"q1": "診断確定日"}, {"q1": [_CITATION]})
    urls = [e["url"] for e in out["item"][0]["extension"]]
    assert urls == ["http://example.org/other", CLINICAL_ANNOTATION_EXTENSION_URL]


def test_citations_without_an_annotation_are_not_embedded() -> None:
    """記述が無い項目には典拠だけを載せないこと（記述が主で典拠は従）。"""
    out, _ = embed_clinical_annotations(_content(), {}, {"q1": [_CITATION]})
    assert "extension" not in out["item"][0]


def test_the_embed_count_is_what_was_actually_embedded() -> None:
    """返す件数が**実際に埋め込んだ数**であること（手元の件数ではない）。

    配布できない典拠は落ちるので、手元の数をそのまま報告すると監査ログと API 応答が
    実態とずれる（ALCOA+ の Accurate）。
    """
    _, embedded = embed_clinical_annotations(
        _content(), {"q1": "診断確定日"}, {"q1": [_CITATION, _HANDWRITTEN]}
    )
    assert embedded == 1

    _, none_embedded = embed_clinical_annotations(
        _content(), {"q1": "診断確定日"}, {"q1": [_HANDWRITTEN]}
    )
    assert none_embedded == 0


# =============================================================================
# 2. 往復で情報が落ちない
# =============================================================================


def test_round_trip_recovers_the_distributed_subset() -> None:
    """書き出した内容が読み戻せること（受け取った施設が得るのは配布された部分集合）。"""
    out, _ = embed_clinical_annotations(_content(), {"q1": "診断確定日"}, {"q1": [_CITATION]})
    extracted = extract_clinical_annotations(out)
    assert extracted.annotations == {"q1": "診断確定日"}
    assert extracted.citations_for("q1") == [
        {
            "unit_id": _UNIT_ID,
            "payload": {
                "page": 12,
                "char_range": [340, 780],
                "mime": "application/pdf",
                "path": "manuals/登録の手引き.pdf",
                # **`file_role` は復元されない。** 配布物に無いものを補うのは由来の捏造。
                # 代わりに「配布物から入った」という事実を来歴として書く（再配布の根拠）。
                "origin": ORIGIN_IMPORTED,
            },
            "llm_model": "qwen3:8b",
        }
    ]


def test_round_trip_is_stable_when_repeated() -> None:
    """読み戻したものを書き出し直しても同じ Extension になること（非可逆化の検出）。"""
    first, _ = embed_clinical_annotations(_content(), {"q1": "診断確定日"}, {"q1": [_CITATION]})
    extracted = extract_clinical_annotations(first)
    second, _ = embed_clinical_annotations(
        _content(),
        extracted.annotations,
        {lid: extracted.citations_for(lid) or [] for lid in extracted.link_ids_with_citations()},
    )
    assert second == first


def test_nested_items_travel_too() -> None:
    """入れ子の item（group の子）にも埋め込まれ、抽出できること。"""
    content = _content()
    content["item"][0]["item"] = [{"linkId": "q1a", "text": "子項目", "type": "string"}]
    out, _ = embed_clinical_annotations(
        content, {"q1": "親の記述", "q1a": "子の記述"}, {"q1a": [_CITATION]}
    )
    extracted = extract_clinical_annotations(out)
    assert extracted.annotations == {"q1": "親の記述", "q1a": "子の記述"}
    # 複合形式なので典拠0件の項目にもキーは立つ（「運ばない形式」との区別）
    assert extracted.citations_for("q1") == []
    assert [c["unit_id"] for c in extracted.citations_for("q1a")] == [_UNIT_ID]


def test_merge_keeps_the_local_snippet() -> None:
    """自分の書き出しを読み戻したとき、配布に載らない手元の値が消えないこと。

    これが無いと「書き出し→読み込み」で規約文書の抜粋が失われる（自施設のデータが
    配布の都合で削られる）。
    """
    out, _ = embed_clinical_annotations(_content(), {"q1": "診断確定日"}, {"q1": [_CITATION]})
    extracted = extract_clinical_annotations(out)
    # 手元は古いページ番号を持っている。配布側（新しい書き出し）が勝つべき。
    stale = {**_CITATION, "payload": {**_FULL_PAYLOAD, "page": 99}}
    merged = _merge_imported_citations([stale], extracted.citations_for("q1"))
    assert merged[0]["payload"]["page"] == 12, "配布側の値で更新されていない"
    assert merged[0]["payload"]["snippet"] == _FULL_PAYLOAD["snippet"]
    assert merged[0]["payload"]["connection_id"] == _FULL_PAYLOAD["connection_id"]
    assert merged[0]["payload"]["page"] == 12
    assert merged[0]["llm_model"] == "qwen3:8b"


def test_a_handwritten_citation_survives_the_round_trip() -> None:
    """配布物で表現できない手元の典拠（索引に無い紙資料）が、読み戻しで消えないこと。

    `unit_id` が索引のアドレスでない典拠は Extension に載らない。載らないものを
    「配布側に無い＝消された」と読むと、**自分の書き出しを読み戻しただけで手元の記録が
    消える**（ALCOA+ の原本性）。
    """
    out, _ = embed_clinical_annotations(
        _content(), {"q1": "診断確定日"}, {"q1": [_CITATION, _HANDWRITTEN]}
    )
    extracted = extract_clinical_annotations(out)
    assert len(extracted.citations_for("q1")) == 1, "手書き典拠は配布されない"
    merged = _merge_imported_citations([_CITATION, _HANDWRITTEN], extracted.citations_for("q1"))
    assert _HANDWRITTEN in merged, "手書き典拠が読み戻しで消えた"
    assert len(merged) == 2


def test_duplicate_unit_ids_keep_their_own_local_values() -> None:
    """同じ `unit_id` の典拠が手元に2件あっても、それぞれの手元固有の値を保つこと。

    同じチャンクを2回足せる（UI にも `_clean_citation` にも重複排除は無い）。辞書で
    突き合わせると、先勝ちでも後勝ちでも2件目の抜粋が黙って消える。
    """
    first = {**_CITATION, "payload": {**_FULL_PAYLOAD, "snippet": "抜粋その1"}}
    second = {**_CITATION, "payload": {**_FULL_PAYLOAD, "snippet": "抜粋その2"}}
    out, embedded = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": [first, second]})
    assert embedded == 2
    extracted = extract_clinical_annotations(out)
    merged = _merge_imported_citations([first, second], extracted.citations_for("q1") or [])
    assert [c["payload"]["snippet"] for c in merged] == ["抜粋その1", "抜粋その2"]


def test_the_confirmation_key_ignores_citations_that_cannot_travel() -> None:
    """確認の再判定に、配布できない典拠を含めないこと。

    含めると、読み込みのたびに「典拠が変わった」と判定されて確認が外れ続ける。
    """
    assert _citation_keys([_CITATION, _HANDWRITTEN]) == [_UNIT_ID]


def test_the_confirmation_key_is_order_sensitive() -> None:
    """典拠の**並び**も確認の対象であること（提示順は人が見た内容の一部）。"""
    other = {
        "unit_id": "file:manuals/別の手引き.pdf#p3:0-100",
        "payload": {"file_role": "reference", "path": "manuals/別の手引き.pdf"},
        "llm_model": None,
    }
    assert _citation_keys([_CITATION, other]) != _citation_keys([other, _CITATION])


def test_merge_prefers_the_distributed_llm_model() -> None:
    """来歴（`llm_model`）は配布側が正で、無いときだけ手元を残すこと。"""
    mine = {**_CITATION, "llm_model": "手元の古いモデル"}
    incoming_with = [{**_CITATION, "llm_model": "配布側の新しいモデル"}]
    assert _merge_imported_citations([mine], incoming_with)[0]["llm_model"] == (
        "配布側の新しいモデル"
    )
    incoming_without = [{**_CITATION, "llm_model": None}]
    assert _merge_imported_citations([mine], incoming_without)[0]["llm_model"] == "手元の古いモデル"


def test_merge_takes_the_incoming_set_and_order() -> None:
    """手元にしか無い典拠は残さないこと（配布側が正）。順序も配布側に合わせる。"""
    other = {
        "unit_id": "file:manuals/別の文書.pdf#p1:0-100",
        "payload": {"file_role": "reference"},
        "llm_model": None,
    }
    merged = _merge_imported_citations([_CITATION, other], [other, {**_CITATION, "payload": None}])
    assert _citation_keys(merged) == [other["unit_id"], _UNIT_ID]


def test_merge_on_a_receiving_facility_keeps_what_was_sent() -> None:
    """手元に典拠が無い施設では、配布された内容がそのまま入ること。"""
    out, _ = embed_clinical_annotations(_content(), {"q1": "診断確定日"}, {"q1": [_CITATION]})
    extracted = extract_clinical_annotations(out)
    assert _merge_imported_citations([], extracted.citations_for("q1")) == extracted.citations_for(
        "q1"
    )


# =============================================================================
# 3. 旧形式（valueString 単一値）を読める
# =============================================================================


def test_the_legacy_single_value_form_is_still_readable() -> None:
    """複合化以前の単一値形式で書き出された Questionnaire を読み込めること（典拠は空）。"""
    content = _content()
    content["item"][0]["extension"] = [
        {"url": CLINICAL_ANNOTATION_EXTENSION_URL, "valueString": "旧形式の記述"}
    ]
    extracted = extract_clinical_annotations(content)
    assert extracted.annotations == {"q1": "旧形式の記述"}
    assert extracted.citations_for("q1") is None
    assert extracted.legacy_link_ids == frozenset({"q1"})


def test_legacy_form_is_distinguishable_from_zero_citations() -> None:
    """「典拠が無い」と「典拠を運ばない形式だった」を区別できること。

    旧形式の Questionnaire を読み込んだだけで手元の典拠が消えるのは、情報を運んでいない
    ものを「空だ」と解釈する誤りである。取り込み側はこの区別で挙動を変える。
    """
    legacy = _content()
    legacy["item"][0]["extension"] = [
        {"url": CLINICAL_ANNOTATION_EXTENSION_URL, "valueString": "旧形式の記述"}
    ]
    legacy_extracted = extract_clinical_annotations(legacy)
    assert legacy_extracted.citations_for("q1") is None  # 運んでいない
    assert legacy_extracted.legacy_link_ids == frozenset({"q1"})

    compound, _ = embed_clinical_annotations(_content(), {"q1": "記述"}, {"q1": []})
    compound_extracted = extract_clinical_annotations(compound)
    assert compound_extracted.citations_for("q1") == []  # 0件だと言っている
    assert compound_extracted.legacy_link_ids == frozenset()


def test_a_broken_extension_does_not_raise() -> None:
    """壊れた Extension（値も下位要素も無い／型違い）で例外にならないこと。"""
    content = _content(("q1", "q2", "q3"))
    content["item"][0]["extension"] = [{"url": CLINICAL_ANNOTATION_EXTENSION_URL}]
    content["item"][1]["extension"] = [
        {"url": CLINICAL_ANNOTATION_EXTENSION_URL, "extension": ["文字列", None]}
    ]
    content["item"][2]["extension"] = [
        {
            "url": CLINICAL_ANNOTATION_EXTENSION_URL,
            "extension": [
                {"url": SUB_ANNOTATION, "valueString": "生きている記述"},
                {"url": SUB_CITATION, "extension": [{"url": SUB_CITATION_PAGE, "valueInteger": 3}]},
            ],
        }
    ]
    extracted = extract_clinical_annotations(content)
    assert extracted.annotations == {"q3": "生きている記述"}
    # unit_id が無くてもページだけの典拠は拾う（情報を捨てない）
    assert extracted.citations_for("q3")[0]["payload"] == {"page": 3}


# =============================================================================
# 4. StructureDefinition と実装がずれない
# =============================================================================


def _sd() -> dict[str, Any]:
    return json.loads(_SD_PATH.read_text(encoding="utf-8"))


def test_structure_definition_exists_and_matches_the_extension_url() -> None:
    """SD が存在し、その `url` が実装の Extension URL と一致すること。"""
    sd = _sd()
    assert sd["resourceType"] == "StructureDefinition"
    assert sd["type"] == "Extension"
    assert sd["url"] == CLINICAL_ANNOTATION_EXTENSION_URL
    assert sd["context"][0]["expression"] == "Questionnaire.item"


def test_structure_definition_declares_exactly_the_sub_extension_names() -> None:
    """SD の下位要素名の集合が、実装が書き出す名前と一致すること（両方向のドリフト）。

    SD は「配布物の読み方」を受け取った施設に伝える唯一の定義なので、実装だけ増えると
    受け手は未定義の要素を読むことになり、SD だけ増えると存在しない要素を待つことになる。
    """
    elements = _sd()["differential"]["element"]
    # `Extension.url` の固定は Extension 自身の URL（バリデータが要求する）で、下位要素の
    # 名前ではない。別に確かめる（次のテスト）
    fixed = {
        e["fixedUri"]
        for e in elements
        if e.get("fixedUri") is not None and e["id"] != "Extension.url"
    }
    assert fixed == {
        SUB_ANNOTATION,
        SUB_CITATION,
        SUB_CITATION_UNIT_ID,
        SUB_CITATION_SOURCE,
        SUB_CITATION_PAGE,
        SUB_CITATION_CHAR_RANGE,
        SUB_CITATION_MIME,
        SUB_CITATION_LLM_MODEL,
    }


def test_the_extension_url_is_fixed_to_the_canonical_url() -> None:
    """`Extension.url` が canonical URL に固定されていること（HL7 FHIR Validator の要求）。

    固定が無いと「Extension.url の値が extension URL に固定されていません」の error になる。
    実装が書き出す URL と SD の固定値がずれると、受け手は別の Extension として読む。
    """
    by_id = {e["id"]: e for e in _sd()["differential"]["element"]}
    assert by_id["Extension.url"]["fixedUri"] == CLINICAL_ANNOTATION_EXTENSION_URL


def test_the_two_level_slicing_is_declared_explicitly() -> None:
    """複合 Extension の2段ネストに、明示的な `slicing` 宣言があること（HL7 FHIR Validator の要求）。

    `Extension.extension` と `Extension.extension:citation.extension` の両方に無いと、
    バリデータが citation の下位スライスを snapshot に展開できず error になる（実測）。
    """
    by_id = {e["id"]: e for e in _sd()["differential"]["element"]}
    for element_id in ("Extension.extension", "Extension.extension:citation.extension"):
        slicing = by_id[element_id].get("slicing")
        assert slicing and slicing["discriminator"] == [{"type": "value", "path": "url"}], (
            element_id
        )


def test_the_complex_extension_url_comes_after_its_child_slices() -> None:
    """複合 Extension の `.url` は、子スライスより**後**に置くこと（HL7 FHIR Validator の要求）。

    前に置くと snapshot に載らず、バリデータが "No match found" の error にする（実測）。
    """
    ids = [e["id"] for e in _sd()["differential"]["element"]]
    last_child = max(
        i for i, v in enumerate(ids) if v.startswith("Extension.extension:citation.extension")
    )
    assert ids.index("Extension.extension:citation.url") > last_child
    assert ids.index("Extension.url") > ids.index("Extension.extension:citation.url")


_FHIR_TYPE_TO_VALUE_KEY = {"string": "valueString", "integer": "valueInteger", "code": "valueCode"}


def _sd_declared_value_keys() -> dict[str, str]:
    """SD が宣言する {下位要素名: 期待する value[x] のキー} を読む。"""
    elements = _sd()["differential"]["element"]
    by_slice: dict[str, str] = {}
    for element in elements:
        if not element["id"].endswith(".value[x]"):
            continue
        slice_id = element["id"].rsplit(".value[x]", 1)[0]
        name = slice_id.rsplit(":", 1)[-1]
        types = element.get("type") or []
        if types:
            by_slice[name] = _FHIR_TYPE_TO_VALUE_KEY[types[0]["code"]]
    return by_slice


def test_the_emitted_extension_matches_the_types_the_sd_declares() -> None:
    """**実際に書き出した Extension** が SD の宣言（名前・型）と一致すること。

    名前の集合だけを見る検査では、型を変える退行（`page` を `valueString` で出す、SD が
    `mime` を `code` と宣言しているのに実装が `valueString` を書く）が通る。SD どおりに
    読む受け手はその要素を見つけられないので、これは配布物の壊れである。
    """
    out, _ = embed_clinical_annotations(_content(), {"q1": "診断確定日"}, {"q1": [_CITATION]})
    declared = _sd_declared_value_keys()
    ext = _extension_of(out)
    emitted: list[tuple[str, dict[str, Any]]] = [
        (e["url"], e) for e in ext["extension"] if e["url"] != SUB_CITATION
    ]
    for citation in _sub(ext, SUB_CITATION):
        emitted.extend((e["url"], e) for e in citation["extension"])

    assert emitted, "書き出しが空（フィクスチャが検査になっていない）"
    for name, element in emitted:
        assert name in declared, f"{name} は SD に宣言が無い"
        expected = declared[name]
        assert expected in element, f"{name}: SD は {expected} を宣言しているが {element}"


def test_structure_definition_bounds_the_slices() -> None:
    """記述は1件必須・典拠は0件以上、というカード数が SD に書かれていること。

    ここが緩むと、記述の無い Extension や典拠1件までの Extension も「準拠」になる。
    """
    elements = {e["id"]: e for e in _sd()["differential"]["element"]}
    annotation = elements["Extension.extension:annotation"]
    assert (annotation["min"], annotation["max"]) == (1, "1")
    citation = elements["Extension.extension:citation"]
    assert (citation["min"], citation["max"]) == (0, "*")


def test_structure_definition_forbids_a_value_on_the_root_extension() -> None:
    """複合 Extension が値を持たないことが SD に書かれていること。"""
    root_value = next(
        e for e in _sd()["differential"]["element"] if e["id"] == "Extension.value[x]"
    )
    assert root_value["max"] == "0"


def test_the_allowlist_is_the_one_the_sub_extensions_are_built_from() -> None:
    """許可キーの集合が**下位要素を作る表そのもの**から導出されていること。

    許可リストを別に書くと、リストから外しても出力側が別経路で出し続ける形が作れる
    （守っているつもりで守っていない）。表から導出していれば、配布から外す＝表から
    行を消すことになり、出力も同時に消える。
    """
    assert EXPORTABLE_CITATION_PAYLOAD_KEYS == frozenset(_CITATION_PART_BUILDERS)
    assert EXPORTABLE_CITATION_PAYLOAD_KEYS == frozenset({"page", "char_range", "mime"})
    assert "snippet" not in EXPORTABLE_CITATION_PAYLOAD_KEYS
    assert "connection_id" not in EXPORTABLE_CITATION_PAYLOAD_KEYS
    assert "label" not in EXPORTABLE_CITATION_PAYLOAD_KEYS
    # **`path` も payload からは出さない。** 受け手へは検証済みの `unit_id` と、そこから
    # 組み立てた `source` を通じて届く。保存 API から書ける `payload["path"]` は配布に
    # 一切関与しない（関与させると `unit_id` の形式検査を迂回できる）。
    assert "path" not in EXPORTABLE_CITATION_PAYLOAD_KEYS
    # `file_role` は値を出すためではなく配布可否の判定に使う
    assert "file_role" not in EXPORTABLE_CITATION_PAYLOAD_KEYS


def test_the_design_doc_states_what_is_not_distributed() -> None:
    """配布しないものが設計文書に書かれていること。

    許可リストはコードにあるが、**何を配布しないと決めたか**は施設が読む文書に無いと
    運用判断ができない（規約文書の抜粋が出ないことは、索引に何を置いてよいかの前提でもある）。
    """
    doc = (_REPO_ROOT / "docs/ai_design_two_roles.md").read_text(encoding="utf-8")
    # 段落を1つの文字列に畳んでから見る（改行位置の変更で落ちないように）。**否定と対象が
    # 同じ文の中にある**ことを見るのが要点で、「どこかに両方ある」では反転を検出できない。
    paragraphs = [p.replace("\n", "") for p in doc.split("\n\n")]
    stated = [
        p for p in paragraphs if "payload.snippet" in p and "connection_id" in p and "載せない" in p
    ]
    assert stated, "配布しないものが1つの段落で述べられていない"
    # 出典表記を組み立て直す方針も文書に置く（保存値をそのまま出さない理由）
    assert any("組み立て直す" in p and "保存値" in p for p in paragraphs)


# =============================================================================
# ルート越し（export → import）の往復
# =============================================================================


async def _make_questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-clinical-ext",
        title="Extension往復テスト票",
        status="active",
        content=_content(("q1",)),
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


def _act_as(client: AsyncClient, user: User) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": RoleType.DESIGNER})
    client.cookies.set(settings.jwt_cookie_name, token)


@pytest.mark.asyncio
async def test_export_then_import_through_the_routes_preserves_everything(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """ルート越しの往復で、確認済みのまま・手元の抜粋も典拠も残ること。

    自分の書き出しを読み戻しただけで確認が外れるなら、配布機能は運用で使えない。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user)
    db_session.add(
        ClinicalAnnotation(
            questionnaire_id=q.id,
            link_id="q1",
            item_text="項目q1",
            annotation="診断確定日",
            citations=[_CITATION],
            status=AnnotationStatus.CONFIRMED,
            confirmed_by_id=designer_user.id,
        )
    )
    await db_session.commit()

    base = f"/ai-input/questionnaires/{q.id}/source-survey/clinical"
    resp = await designer_client.post(
        f"{base}/export-extension", headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["embedded"] == 1
    assert resp.json()["embedded_citations"] == 1

    resp = await designer_client.post(
        f"{base}/import-extension", headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported_citations"] == 1

    row = (
        await db_session.execute(
            select(ClinicalAnnotation).where(
                ClinicalAnnotation.questionnaire_id == q.id,
                ClinicalAnnotation.link_id == "q1",
            )
        )
    ).scalar_one()
    assert row.annotation == "診断確定日"
    assert row.citations[0]["payload"]["snippet"] == _FULL_PAYLOAD["snippet"]
    assert row.citations[0]["unit_id"] == _UNIT_ID
    # 自分の書き出しの読み戻しは内容を変えないので、確認は外れない
    assert row.status == AnnotationStatus.CONFIRMED
    assert row.confirmed_by_id == designer_user.id


@pytest.mark.asyncio
async def test_export_reports_and_keeps_citations_it_cannot_distribute(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """ルート越しでも、配布できない典拠が(1)応答で報告され (2)読み戻しで消えないこと。

    配布物に載らない典拠があること自体は異常ではない（索引に無い紙資料を人が書ける）。
    黙って落として黙って消すのが問題なので、件数を返し、手元には残す。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user)
    db_session.add(
        ClinicalAnnotation(
            questionnaire_id=q.id,
            link_id="q1",
            item_text="項目q1",
            annotation="診断確定日",
            citations=[_CITATION, _HANDWRITTEN],
            status=AnnotationStatus.CONFIRMED,
            confirmed_by_id=designer_user.id,
        )
    )
    await db_session.commit()

    base = f"/ai-input/questionnaires/{q.id}/source-survey/clinical"
    resp = await designer_client.post(
        f"{base}/export-extension", headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["embedded_citations"] == 1
    assert resp.json()["not_distributable_citations"] == 1

    resp = await designer_client.post(
        f"{base}/import-extension", headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(
            select(ClinicalAnnotation).where(
                ClinicalAnnotation.questionnaire_id == q.id,
                ClinicalAnnotation.link_id == "q1",
            )
        )
    ).scalar_one()
    assert len(row.citations) == 2, "配布できない典拠が読み戻しで消えた"
    assert _HANDWRITTEN in row.citations
    # 手元の内容は変わっていないので確認は外れない
    assert row.status == AnnotationStatus.CONFIRMED


@pytest.mark.asyncio
async def test_a_receiving_facility_gets_the_citations_on_first_import(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """手元に記述が無い施設が読み込むと、記述と**典拠**の両方が入ること。

    受け取り側にとってはこれが複合化の目的そのもの（配布された記述の根拠を辿れる）。
    手元に行がある場合の経路とは別なので、単独で見る。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user)
    q.content, _ = embed_clinical_annotations(q.content, {"q1": "診断確定日"}, {"q1": [_CITATION]})
    await db_session.commit()

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/clinical/import-extension",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True, "imported": 1, "imported_citations": 1}

    row = (
        await db_session.execute(
            select(ClinicalAnnotation).where(
                ClinicalAnnotation.questionnaire_id == q.id,
                ClinicalAnnotation.link_id == "q1",
            )
        )
    ).scalar_one()
    assert row.annotation == "診断確定日"
    assert [c["unit_id"] for c in row.citations] == [_UNIT_ID]
    assert row.citations[0]["payload"]["page"] == 12
    # 配布に載らないものは受け取り側にも無い
    assert "snippet" not in (row.citations[0]["payload"] or {})


@pytest.mark.asyncio
async def test_reordering_the_citations_resets_the_confirmation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """典拠の**並べ替えだけ**でも確認が外れること。

    提示順は人が確認した内容の一部（どれを主たる根拠として読むかが変わる）。集合として
    比べる実装だと、この差が見えない。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user)
    other = {
        "unit_id": "file:manuals/別の手引き.pdf#p3:0-100",
        "payload": {"file_role": "reference", "path": "manuals/別の手引き.pdf", "page": 3},
        "llm_model": None,
    }
    q.content, _ = embed_clinical_annotations(
        q.content, {"q1": "診断確定日"}, {"q1": [other, _CITATION]}
    )
    db_session.add(
        ClinicalAnnotation(
            questionnaire_id=q.id,
            link_id="q1",
            item_text="項目q1",
            annotation="診断確定日",
            citations=[_CITATION, other],  # 同じ2件だが並びが逆
            status=AnnotationStatus.CONFIRMED,
            confirmed_by_id=designer_user.id,
        )
    )
    await db_session.commit()

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/clinical/import-extension",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(
            select(ClinicalAnnotation).where(
                ClinicalAnnotation.questionnaire_id == q.id,
                ClinicalAnnotation.link_id == "q1",
            )
        )
    ).scalar_one()
    assert [c["unit_id"] for c in row.citations] == [other["unit_id"], _UNIT_ID]
    assert row.status == AnnotationStatus.DRAFT
    assert row.confirmed_by_id is None


@pytest.mark.asyncio
async def test_importing_a_legacy_questionnaire_keeps_local_citations(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """旧形式（典拠を運ばない）の読み込みで、手元の典拠が消えないこと。

    運んでいないものを「空だ」と解釈すると、複合化以前の単一値形式で書き出された Questionnaire を
    読み込んだだけで典拠が全部消える。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user)
    q.content["item"][0]["extension"] = [
        {"url": CLINICAL_ANNOTATION_EXTENSION_URL, "valueString": "外から来た別の記述"}
    ]
    db_session.add(
        ClinicalAnnotation(
            questionnaire_id=q.id,
            link_id="q1",
            item_text="項目q1",
            annotation="診断確定日",
            citations=[_CITATION],
            status=AnnotationStatus.CONFIRMED,
            confirmed_by_id=designer_user.id,
        )
    )
    await db_session.commit()

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/clinical/import-extension",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(
            select(ClinicalAnnotation).where(
                ClinicalAnnotation.questionnaire_id == q.id,
                ClinicalAnnotation.link_id == "q1",
            )
        )
    ).scalar_one()
    assert row.annotation == "外から来た別の記述"
    assert row.citations == [_CITATION]  # 典拠はそのまま
    # 記述は変わったので確認は外れる（従来どおり）
    assert row.status == AnnotationStatus.DRAFT


@pytest.mark.asyncio
async def test_import_with_different_citations_resets_the_confirmation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """記述が同じでも**典拠が変われば**確認を外すこと。

    典拠は確認の材料そのもので、根拠が差し替わったのに「確認済み」が残るのは
    ALCOA+ の帰属性に反する（記述だけを見る従来の規則では見逃す）。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user)
    other = {
        "unit_id": "file:manuals/別の手引き.pdf#p3:0-100",
        "payload": {"file_role": "reference", "path": "manuals/別の手引き.pdf", "page": 3},
        "llm_model": None,
    }
    # 外から来た Questionnaire は「別の手引き」を典拠にしている
    q.content, _ = embed_clinical_annotations(q.content, {"q1": "診断確定日"}, {"q1": [other]})
    db_session.add(
        ClinicalAnnotation(
            questionnaire_id=q.id,
            link_id="q1",
            item_text="項目q1",
            annotation="診断確定日",
            citations=[_CITATION],
            status=AnnotationStatus.CONFIRMED,
            confirmed_by_id=designer_user.id,
        )
    )
    await db_session.commit()

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/clinical/import-extension",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(
            select(ClinicalAnnotation).where(
                ClinicalAnnotation.questionnaire_id == q.id,
                ClinicalAnnotation.link_id == "q1",
            )
        )
    ).scalar_one()
    assert row.status == AnnotationStatus.DRAFT
    assert row.confirmed_by_id is None
    assert _citation_keys(row.citations) == [other["unit_id"]]
