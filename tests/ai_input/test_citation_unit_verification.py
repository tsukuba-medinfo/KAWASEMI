"""保存時に典拠の `unit_id` を索引で照合することのテスト。

`unit_id` は UI の自由入力欄（`design_clinical.html` の典拠エディタ）から書ける。形式検査
（`file_export._REFERENCE_UNIT_ID`）は「空白を含まない任意の文字列」を通すので、
**ゼロ幅空白（U+200B）で連結した `file:患者<U+200B>山田太郎:0-0` が通る**。

配布物に出るのは `unitId` とそこから組み立てた `source` である。**配布可否と典拠の付与率の分子は
索引の実在を根拠にする**ので、保存の時点で決着させる（AI と人手の典拠を
同じ基準で比較するには、**保存時**の索引照合が要る）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from kawasemi.ai_input.models import ClinicalAnnotation
from kawasemi.ai_input.resource_index.schema import SCHEMA_VERSION
from kawasemi.ai_input.resource_index.store import reset_store_cache
from kawasemi.ai_input.routes import _normalize_citations
from kawasemi.core.config import settings
from kawasemi.utils.file_export import ORIGIN_IMPORTED, is_distributable_citation

_UNIT_ID = "file:manual.pdf#p12:0-800"
# **索引照合の層を測るための捏造アドレス**。形式検査を通り、索引には
# 無く、**書式文字を含まない**。以前はここに `_FORMAT_CHAR_UNIT_ID` を使っていたので、
# `has_forbidden_unit_id_chars` が**先に**落としており、**索引照合は1件も測れていなかった**
# （先頭1件だけ照合する退行が素通りする）。
_FABRICATED_UNIT_ID = "file:捏造マニュアル.pdf#p1:0-1"
# 形式検査は通るが**書式文字**（U+200B）を含む＝索引照合とは**別の層**で落ちるアドレス
_FORMAT_CHAR_UNIT_ID = "file:患者​山田太郎:0-0"


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    reset_store_cache()
    yield
    reset_store_cache()


def _write_index(base: Path) -> str:
    """`_UNIT_ID` を1件だけ含む、ロードできる索引を作る。"""
    base.mkdir(parents=True, exist_ok=True)
    (base / "build_info.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "indexer_version": "0.1.0",
                "embedding_model": "bge-m3",
                "dim": 4,
                "normalized": True,
                "unit_count": 1,
                "kind_counts": {"file": 1},
                "built_at": "2026-08-05T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    unit = {
        "unit_id": _UNIT_ID,
        "kind": "file",
        "label": "manual.pdf（p12）",
        "search_text": "診断日は、組織学的または細胞学的に確定した日とする。",
        "snippet": "診断日は、組織学的または",
        "payload": {
            "file_role": "reference",
            "path": "manual.pdf",
            "page": 12,
            "char_range": [0, 800],
            "mime": "application/pdf",
        },
    }
    (base / "file_index.jsonl").write_text(
        json.dumps(unit, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    np.save(base / "embeddings.npy", np.ones((1, 4), dtype=np.float32))
    return str(base)


@pytest.fixture
def index_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = _write_index(tmp_path / "resource_index")
    monkeypatch.setattr(settings, "resource_index_dir", path)
    return path


def _item(**over: Any) -> dict[str, Any]:
    citation: dict[str, Any] = {
        "unit_id": _UNIT_ID,
        "payload": {"file_role": "reference", "path": "捏造.pdf", "page": "99"},
        "llm_model": None,
    }
    citation.update(over)
    return {"link_id": "q1", "citations": [citation]}


def test_the_fabricated_address_is_rejected_only_by_the_index_check() -> None:
    """**前提**: 捏造アドレスは書式文字の層では落ちない。

    索引照合を測るテストが使うアドレスに書式文字が混ざると、`has_forbidden_unit_id_chars`
    が先に落とすので、**索引照合を殺す退行が素通りする**（「先頭1件だけ照合する」
    変異が生き残る）。フィクスチャの性質そのものを assert として置く。
    """
    from kawasemi.utils.file_export import _REFERENCE_UNIT_ID, has_forbidden_unit_id_chars

    assert _REFERENCE_UNIT_ID.match(_FABRICATED_UNIT_ID), "前提: 形式検査は通る"
    assert not has_forbidden_unit_id_chars(_FABRICATED_UNIT_ID), (
        "捏造アドレスが書式文字の層で落ちている（索引照合が測れない）"
    )
    # 対照: 書式文字を含む方は**この層で**落ちる（層が2つあることの確認）
    assert has_forbidden_unit_id_chars(_FORMAT_CHAR_UNIT_ID)


def test_a_citation_that_exists_in_the_index_takes_the_index_payload(index_dir: str) -> None:
    """**索引に実在する典拠は payload を索引から採る**（クライアントの自己申告を採らない）。

    これで**人が正しいアドレスを貼った典拠も分子に入る**（保存時照合の目的）。AI 経路は既に索引由来の
    payload を持っているので、人手入力を同じ位置で同じ規則に揃えることになる。
    """
    [saved] = _normalize_citations(_item())

    assert saved["unit_id"] == _UNIT_ID
    # クライアントが送った嘘（path/page）は採らない
    assert saved["payload"]["path"] == "manual.pdf"
    assert saved["payload"]["page"] == 12, "索引の型（int）のまま採る"
    assert saved["payload"]["file_role"] == "reference"
    assert is_distributable_citation(saved), "実在する典拠が配布できない"


def test_the_index_wins_and_the_allowlisted_client_keys_are_kept(index_dir: str) -> None:
    """**索引を勝たせるが、許可リストのキー（`snippet` / `label`）は残す**。

    索引の payload は `connection_id / file_role / path / page / char_range / mime` だけで、
    **`snippet`（人が編集する「該当箇所の抜粋」）と `label` は入っていない**。残すのは
    **`_CLIENT_PAYLOAD_KEYS` に挙げたこの2つだけ**である（「索引が持たないキーを全部残す」形では
    `origin` の自己申告が生き残る）。丸ごと置き換えると
    3つ同時に壊れる——**人の入力が消える**／`content_unchanged` の全体一致が崩れて
    **他人の確認帰属を奪う**／`citations` が変わるので**下流の手順書・プランが stale 化する**。
    """
    [saved] = _normalize_citations(
        _item(
            payload={
                "file_role": "reference",
                "path": "捏造.pdf",
                "snippet": "人が書いた抜粋",
                "label": "手引き p12",
            }
        )
    )

    # 索引が勝つ（自己申告のパスは採らない）
    assert saved["payload"]["path"] == "manual.pdf"
    assert saved["payload"]["page"] == 12
    # **許可リストのキー（`snippet` / `label`）は残る**（人の入力を消さない）
    assert saved["payload"]["snippet"] == "人が書いた抜粋"
    assert saved["payload"]["label"] == "手引き p12"


def test_saving_the_same_citation_twice_is_idempotent(index_dir: str) -> None:
    """**同じ典拠を2回保存しても値が変わらない**こと（上の規則の帰結）。

    保存のたびに payload が変わると、`routes` の `content_unchanged`（`row.citation_list ==
    citations` の全体一致）が崩れ、**記述もアドレスも変えていないのに確認帰属が奪われ、
    下流が stale 化する**。冪等であることを直接測る。
    """
    once = _normalize_citations(_item())
    twice = _normalize_citations({"link_id": "q1", "citations": [dict(once[0])]})
    assert once == twice, "保存し直すと payload が変わる（確認帰属が奪われる）"


def test_a_fabricated_unit_id_loses_its_claim_of_being_indexed(index_dir: str) -> None:
    """**索引に無い `unit_id` は「索引由来」の主張を失う**（配布物にも分子にも入らない）。

    典拠そのものは残す（人の入力を消さない）。落とすのは主張だけである。
    """
    [saved] = _normalize_citations(_item(unit_id=_FABRICATED_UNIT_ID))

    assert saved["unit_id"] == _FABRICATED_UNIT_ID, "人の入力そのものは残す"
    assert (saved["payload"] or {}).get("file_role") is None, "自己申告の file_role が残っている"
    assert not is_distributable_citation(saved), "索引に無い典拠が配布できてしまう"
    # **同じ形で索引にあるものは通る**（肯定側。常に落とす実装を殺す）
    assert is_distributable_citation(_normalize_citations(_item())[0])


def test_a_self_asserted_import_origin_is_not_honoured(index_dir: str) -> None:
    """**`origin` の自己申告を認めない**。

    `is_distributable_citation` は `payload["origin"] == ORIGIN_IMPORTED` を第2の根拠として
    認める（配布物から取り込んだ典拠は受け取り施設の索引に無いため）。しかしこれは保存 API から
    書けるので、**`origin` を書くだけで捏造した `unit_id` が再配布できる**。
    """
    [saved] = _normalize_citations(
        _item(unit_id=_FABRICATED_UNIT_ID, payload={"origin": ORIGIN_IMPORTED})
    )
    assert not is_distributable_citation(saved), "origin の自己申告で配布できてしまう"


def test_an_imported_origin_already_in_the_db_is_kept(index_dir: str) -> None:
    """**DB にある取り込みの事実は引き継ぐ**（受け取った施設が再配布できなくならないように）。

    引き継ぐ根拠はクライアントの申告ではなく**既存レコード**である。
    **`unit_id` は形式として正しく、この施設の索引には無い**——受け取った施設の実態がこれである
    （書式文字を含むアドレスは `has_forbidden_unit_id_chars` が別の層で落とすので、ここでは
    形の正しいものを使う）。
    """
    imported_id = "file:別施設マニュアル.pdf#p3:0-100"
    existing = ClinicalAnnotation(
        questionnaire_id=1,
        link_id="q1",
        annotation="あ",
        citations=[{"unit_id": imported_id, "payload": {"origin": ORIGIN_IMPORTED}}],
    )
    [saved] = _normalize_citations(
        _item(unit_id=imported_id, payload={"origin": ORIGIN_IMPORTED}), existing=existing
    )
    assert is_distributable_citation(saved), "取り込み由来の典拠が再配布できなくなっている"
    # **別の unit_id には引き継がない**（1件あれば全部通る形にしない）
    [other] = _normalize_citations(
        _item(unit_id="file:別物.pdf#p1:0-1", payload={"origin": ORIGIN_IMPORTED}),
        existing=existing,
    )
    assert not is_distributable_citation(other)


def test_an_unconsultable_index_fails_closed_without_failing_the_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**索引を参照できないときは配布しない側へ倒す。保存は成功する**。

    「照合できないから実在するとみなす」に倒すと、**索引を消すだけで**捏造した `unit_id` が
    配布物と分子に入る。
    """
    monkeypatch.setattr(settings, "resource_index_dir", str(tmp_path / "missing"))
    saved = _normalize_citations(_item())

    assert saved, "保存そのものが失敗している（典拠が消えた）"
    assert (saved[0]["payload"] or {}).get("file_role") is None
    assert not is_distributable_citation(saved[0])


@pytest.mark.parametrize(
    "unit_id",
    [
        _FORMAT_CHAR_UNIT_ID,  # U+200B（ゼロ幅空白。`\s` に含まれない）
        "file:manual\u0378.pdf#p1:0-1",  # U+0378（未割当。Cn）
        "file:manual\u007f.pdf#p1:0-1",  # U+007F（DEL。Cc。正規表現の \\x00-\\x1f は通す）
        "file:manual\ue000.pdf#p1:0-1",  # U+E000（私用領域。Co）
        "file:manual\ud800.pdf#p1:0-1",  # U+D800（孤立サロゲート。Cs）
        "file:manual‎.pdf#p1:0-1",  # U+200E（左横書き制御。Cf）
        "file:manual﻿.pdf#p1:0-1",  # U+FEFF（BOM。Cf）
    ],
)
def test_format_characters_are_rejected_even_without_an_index(unit_id: str) -> None:
    """**書式文字を含むアドレスは索引が無くても弾く**（層を2つ持つ）。

    `_REFERENCE_UNIT_ID` の否定クラス（`[^\\s\\x00-\\x1f\\u3000#]`）は**これらを通す**——
    Python の `\\s` は Unicode の空白だけで `Cf`（format）を含まない。索引の実在確認が入った後も
    この層を置くのは、**索引を参照できない環境ではこちらだけが働く**からである。
    """
    from kawasemi.utils.file_export import _REFERENCE_UNIT_ID, has_forbidden_unit_id_chars

    assert _REFERENCE_UNIT_ID.match(unit_id), "前提: 形式検査は通ってしまう"
    # 対照: **U+00A0（Zs）は既存のパターンが落とす**（Python の `\s` は Unicode の空白を含む）。
    # 落ちているのは `Cf`（書式文字）だけなので、そこを塞ぐのがこの層の役目である
    assert not _REFERENCE_UNIT_ID.match("file:manual\u00a0.pdf#p1:0-1")
    assert has_forbidden_unit_id_chars(unit_id)
    assert not is_distributable_citation(
        {"unit_id": unit_id, "payload": {"file_role": "reference"}}
    )
    # **正常系では発火しない**（普通のアドレスと日本語のパスは通る）
    assert not has_forbidden_unit_id_chars("file:manual.pdf#p12:0-800")
    assert not has_forbidden_unit_id_chars("file:登録マニュアル.pdf#p12:0-800")
    assert is_distributable_citation(
        {"unit_id": "file:manual.pdf#p12:0-800", "payload": {"file_role": "reference"}}
    )


def test_the_forbidden_categories_are_all_rejected_by_the_predicate() -> None:
    """**禁止カテゴリ8件すべて**を述語の側で測る。

    `is_distributable_citation` からの経路では、`Zs` / `Zl` / `Zp` は
    `_REFERENCE_UNIT_ID` の `\\s` が**先に落とす**ので**等価変異**になる（カテゴリを落としても
    答えが変わらない）。上の parametrize は「形式検査を通ってしまう」ことを前提に置いている
    ので、そこには入れられない——**述語を直接呼んで、この層としての会計を閉じる**。

    唯一の砦がこの層なのは `Cc`(U+007F) / `Cf` / `Cn` / `Co` / `Cs` の5件で、上の
    parametrize がその5件を配布可否まで通して測っている。
    """
    from kawasemi.utils.file_export import _REFERENCE_UNIT_ID, has_forbidden_unit_id_chars

    only_this_layer = {
        "Cc": "\u007f",
        "Cf": "\u200b",
        "Cn": "\u0378",
        "Co": "\ue000",
        "Cs": "\ud800",
    }
    also_dropped_by_the_regex = {"Zs": "\u00a0", "Zl": "\u2028", "Zp": "\u2029"}

    for category, char in {**only_this_layer, **also_dropped_by_the_regex}.items():
        unit_id = f"file:manual{char}.pdf#p1:0-1"
        assert has_forbidden_unit_id_chars(unit_id), f"{category} が落ちていない"

    # **どちらの群かを assert として書く**（等価変異になる理由を字面で残す）
    for char in only_this_layer.values():
        assert _REFERENCE_UNIT_ID.match(f"file:manual{char}.pdf#p1:0-1"), "前提: 形式検査は通る"
    for char in also_dropped_by_the_regex.values():
        assert not _REFERENCE_UNIT_ID.match(f"file:manual{char}.pdf#p1:0-1"), (
            "前提: 形式検査が先に落とす"
        )


def test_a_self_asserted_origin_is_not_kept_even_when_the_unit_exists(index_dir: str) -> None:
    """**索引に実在する典拠でも `origin` の自己申告を残さない**。

    索引に実在する枝で早期 return していたため、`origin` が**そのまま保存されていた**。
    その後で索引が差し替わって当該アドレスが消えると、`imported_unit_ids` が
    それを「取り込みの事実」として引き継ぎ、**索引に無いアドレスが配布物と分子に入る**
    ——という洗浄が成立していた。
    """
    [saved] = _normalize_citations(
        _item(payload={"origin": ORIGIN_IMPORTED, "snippet": "抜粋", "arbitrary": "x" * 50})
    )
    assert saved["payload"].get("origin") is None, (
        "自己申告の origin が保存されている（洗浄が成立する）"
    )
    # **許可リスト以外のキーも残さない**（索引が持たないキーを無条件に残していた）
    assert "arbitrary" not in saved["payload"], "任意のキーが保存されている"
    assert saved["payload"]["snippet"] == "抜粋", "人が書いた抜粋は残す"


def test_the_index_wins_over_a_conflicting_self_assertion(index_dir: str) -> None:
    """**索引の値が競合するクライアントの申告に勝つ**こと。

    従来のフィクスチャは `file_role: "reference"` を送っており、**索引の値と自己申告が同値**
    だった。そのため「索引の `file_role` だけ採らない」形と「自己申告を勝たせる」形が
    どちらも素通りした。**配布可否と分子を決める唯一のキー**なので、競合する値を送って測る。
    """
    [saved] = _normalize_citations(
        _item(payload={"file_role": "patient_data", "page": "99", "path": "捏造.pdf"})
    )
    assert saved["payload"]["file_role"] == "reference", "自己申告が索引に勝っている"
    assert saved["payload"]["page"] == 12
    assert saved["payload"]["path"] == "manual.pdf"
    assert is_distributable_citation(saved)


def test_an_unconsultable_index_does_not_destroy_the_existing_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**索引を参照できないだけのときは DB の事実を壊さない**。

    参照不能で `file_role` を消すと `citations` が変わり、記述もアドレスも変えていない保存で
    **他人の確認帰属を奪い、下流を stale 化する**（確認帰属を守る規則で塞いだ害の再発。しかも**AI 由来の典拠も
    巻き込む**）。「照合できない」と「照合したが無かった」を分ける。
    """
    monkeypatch.setattr(settings, "resource_index_dir", str(tmp_path / "missing"))
    existing = ClinicalAnnotation(
        questionnaire_id=1,
        link_id="q1",
        annotation="あ",
        citations=[
            {"unit_id": _UNIT_ID, "payload": {"file_role": "reference", "path": "manual.pdf"}}
        ],
    )
    [saved] = _normalize_citations(_item(), existing=existing)
    assert saved["payload"]["file_role"] == "reference", "参照不能で DB の来歴を消している"
    assert is_distributable_citation(saved)
    # **新しい主張は認めない**（DB に無い unit_id は参照不能でも配布不可のまま）
    [other] = _normalize_citations(_item(unit_id="file:別物.pdf#p1:0-1"), existing=existing)
    assert not is_distributable_citation(other)


def test_every_citation_in_the_list_is_verified(index_dir: str) -> None:
    """**配列の全件が照合される**こと。

    保存 API は1項目に複数典拠を受け、UI にも「典拠を追加」がある。

    **索引にあるユニットを先頭と末尾の両方に置き、捏造を中間に挟む**のが要点である。
    次の2つの並びはどちらも空振りする。

    - `[索引にある, 捏造]`: 2件目が捏造なので**照合してもしなくても答えが同じ**で
      （`unit=None` で規則3 に落ち、どちらでも `file_role` が剥がれる）、「**先頭だけ**照合する」
      変異がリポジトリ全体を素通りする
    - `[捏造, 索引にある]`: 殺せる向きが**反転しただけ**で、
      「**末尾だけ**照合する」変異（`i == len(cleaned) - 1`）が素通りする。単一典拠のテストでは
      位置0 が同時に末尾なので、どれも助けにならない

    **`file_role` を競合させる**（`patient_data` を送る）のも要点である。索引と同値
    （`reference`）だと、索引を引かない形が素通りする（競合する値で測るのと同じ理由）。
    """
    indexed = {"unit_id": _UNIT_ID, "payload": {"file_role": "patient_data"}, "llm_model": None}
    item = {
        "link_id": "q1",
        "citations": [
            dict(indexed),
            {
                "unit_id": _FABRICATED_UNIT_ID,
                "payload": {"file_role": "reference"},
                "llm_model": None,
            },
            dict(indexed),
        ],
    }
    first, middle, last = _normalize_citations(item)

    # 中間（索引に無い）は主張を落とす
    assert not is_distributable_citation(middle)
    assert (middle["payload"] or {}).get("file_role") is None
    # **先頭も末尾も索引と照合される**——照合されなければ索引の payload が入らず、自己申告の
    # `patient_data` が残って配布不可になる（＝照合の有無が答えを変える位置に置いてある）
    for label, saved in (("先頭", first), ("末尾", last)):
        assert saved["payload"]["file_role"] == "reference", f"{label}が照合されていない"
        assert saved["payload"]["page"] == 12, f"{label}が照合されていない"
        assert saved["payload"]["path"] == "manual.pdf", f"{label}が照合されていない"
        assert is_distributable_citation(saved), f"{label}が配布できない"


def test_a_citation_without_a_unit_id_is_kept(index_dir: str) -> None:
    """**抜粋だけの典拠（`unit_id` が無い）を捨てない**こと。

    `_clean_citation` は `unit_id=None` / `payload={"snippet": ...}` を正当な保存対象として通し、
    UI も抜粋だけの行を送れる（紙の資料を典拠にした入力）。型ガードを外しても・捨てる形へ
    変えても、保存経路のテストに1件も無かったので素通りした。
    """
    [saved] = _normalize_citations(
        {"link_id": "q1", "citations": [{"unit_id": None, "payload": {"snippet": "紙の資料 p.7"}}]}
    )
    assert saved["unit_id"] is None
    assert saved["payload"]["snippet"] == "紙の資料 p.7"
    assert not is_distributable_citation(saved)


def test_a_swapped_index_does_not_destroy_the_record(
    index_dir: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**索引が差し替わって当該アドレスが消えても記録を壊さない**。

    「**参照不能のときだけ** `file_role` を復元する」形には、2つの穴がある。
    (a) 復元するのが `file_role` だけで `path` / `page` / `char_range` / `mime` が落ち、
    `content_unchanged` が偽になって**他人の確認帰属を奪う**。(b) **索引の差し替え**
    （デモの索引再生成が本番の索引を上書きするなど）では索引は参照できるので
    復元枝が発火せず、同じ害が起きる。

    条件を分けたことが穴だったので、**照合できなかった `unit_id` が既存レコードにあるなら
    payload をそのまま残す**（参照不能でも差し替えでも同じ答えになる）。
    """
    recorded = {
        "file_role": "reference",
        "path": "manual.pdf",
        "page": 12,
        "char_range": [0, 800],
        "mime": "application/pdf",
    }
    existing = ClinicalAnnotation(
        questionnaire_id=1,
        link_id="q1",
        annotation="あ",
        citations=[{"unit_id": _UNIT_ID, "payload": dict(recorded), "llm_model": "model-a:12b"}],
    )
    # **索引を差し替える**（ロードできるが当該アドレスを持たない索引）
    other = _write_index(tmp_path / "swapped")
    payload_path = Path(other) / "file_index.jsonl"
    payload_path.write_text(
        payload_path.read_text(encoding="utf-8").replace(_UNIT_ID, "file:別物.pdf#p1:0-10"),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "resource_index_dir", other)
    reset_store_cache()

    [saved] = _normalize_citations(_item(), existing=existing)
    assert saved["payload"] == recorded, "索引の差し替えで記録が壊れている（確認帰属を奪う）"
    assert is_distributable_citation(saved)
    # **新しい主張は認めない**（DB に無い `unit_id` は差し替え後も配布不可）
    [other_row] = _normalize_citations(_item(unit_id="file:新規.pdf#p1:0-1"), existing=existing)
    assert not is_distributable_citation(other_row)


def test_the_index_beats_the_recorded_payload_for_the_same_unit_id(index_dir: str) -> None:
    """**索引にある `unit_id` は、既存レコードより索引の値を採る**。

    規則1（索引ヒット）と規則2（記録を残す）の**優先順位**である。従来のフィクスチャは
    規則1 側が `existing=None`、規則2 側が索引に無い `unit_id` だったので、**2つの枝の順序を
    入れ替えても全部緑**だった。

    順序が効くのは**索引に実在するアドレス**についてだけである（規則1 が先に立つので、記録に
    汚れた payload があっても索引の値で上書きされる）。**索引に無いアドレスの記録は規則2 が
    残すので、保存し直しても洗浄されない**（この限界は
    `test_a_record_pointing_outside_the_index_is_never_cleaned_by_saving_again` が固定する）。
    """
    poisoned = {"file_role": "patient_data", "path": "捏造.pdf", "page": 99}
    existing = ClinicalAnnotation(
        questionnaire_id=1,
        link_id="q1",
        annotation="あ",
        citations=[{"unit_id": _UNIT_ID, "payload": dict(poisoned)}],
    )
    [saved] = _normalize_citations(_item(), existing=existing)

    assert saved["payload"]["file_role"] == "reference", (
        "記録が索引に勝っている（再照合が効かない）"
    )
    assert saved["payload"]["path"] == "manual.pdf"
    assert saved["payload"]["page"] == 12
    assert is_distributable_citation(saved)


def test_the_record_is_kept_by_value_not_by_key(index_dir: str) -> None:
    """引き継ぐのは**記録された値そのもの**であること。

    「キーが在るかどうか」で引き継ぐと、`file_role: patient_data` の記録が `reference` へ
    **昇格する**退行が素通りする。
    """
    existing = ClinicalAnnotation(
        questionnaire_id=1,
        link_id="q1",
        annotation="あ",
        citations=[{"unit_id": _FABRICATED_UNIT_ID, "payload": {"file_role": "patient_data"}}],
    )
    [saved] = _normalize_citations(
        _item(unit_id=_FABRICATED_UNIT_ID, payload={"file_role": "reference"}),
        existing=existing,
    )
    assert saved["payload"]["file_role"] == "patient_data", "記録が昇格している"
    assert not is_distributable_citation(saved)


def test_the_first_recorded_payload_wins_when_a_unit_id_appears_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同じ `unit_id` が既存レコードに2件あるとき**先勝ち**であること。

    `_normalize_citations` は `setdefault` で組んでいるが、**後勝ちへ戻す変異が生き残った**。
    重複は実在しうる——`_merge_imported_citations` のコメントが
    「同じ `unit_id` が手元に複数あることはある」と書いており、UI にも `_clean_citation` にも
    重複排除は無い。

    **後勝ちだと緩い記録が勝つ組み合わせが作れる**（`patient_data` の記録のあとに
    `reference` の記録があると、照合できない典拠が配布可能へ昇格する）。`store.get_unit` が
    先勝ちなのと規則を揃える。
    """
    monkeypatch.setattr(settings, "resource_index_dir", str(tmp_path / "missing"))
    existing = ClinicalAnnotation(
        questionnaire_id=1,
        link_id="q1",
        annotation="あ",
        citations=[
            {"unit_id": _FABRICATED_UNIT_ID, "payload": {"file_role": "patient_data"}},
            {"unit_id": _FABRICATED_UNIT_ID, "payload": {"file_role": "reference"}},
        ],
    )
    [saved] = _normalize_citations(
        _item(unit_id=_FABRICATED_UNIT_ID, payload={"file_role": "reference"}), existing=existing
    )
    assert saved["payload"]["file_role"] == "patient_data", "後勝ちで緩い記録が勝っている"
    assert not is_distributable_citation(saved)


def test_a_recorded_unit_id_with_surrounding_spaces_is_still_matched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """既存レコードのキーは **strip して引く**こと。

    `existing_payloads` のキーから `.strip()` を落とす変異は、**リポジトリ全体を素通り**した。
    **等価変異ではない**——取り込み経路
    （`file_export._citation_from_extension`）は `unitId` を strip しないので、**空白つきの
    `unit_id` が DB に入る**。その項目を UI から保存し直すと、strip が無ければ規則2 が発火せず
    取り込み由来の `origin` が落ちる。

    害は索引の差し替えで記録が壊れるときと同じ: その典拠が配布物と分子から抜け、`citations` が
    変わって `content_unchanged` が偽になり**他人の確認帰属を奪う**。フェイルクローズでもないので
    例外もログも出ない。

    **正規化をどの層で行うかは1箇所に決める**——ここ（保存時に既存レコードのキーを strip して
    引く）である。取り込み側では strip しない（配布物の字面をそのまま記録に残す）。
    """
    from kawasemi.utils.file_export import SUB_CITATION_UNIT_ID, _citation_from_extension

    padded = f"  {_FABRICATED_UNIT_ID}  "
    # 取り込み側は `unitId` があれば `origin: imported-extension` を自分で付ける
    imported = _citation_from_extension(
        {"extension": [{"url": SUB_CITATION_UNIT_ID, "valueString": padded}]}
    )
    assert imported is not None
    # **前提**: 取り込み側は strip しない（この前提が崩れたらこのテストは別の性質を測っている）
    assert imported["unit_id"] == padded, "取り込み側が strip している（前提が変わった）"
    assert imported["payload"].get("origin") == ORIGIN_IMPORTED

    monkeypatch.setattr(settings, "resource_index_dir", str(tmp_path / "missing"))
    existing = ClinicalAnnotation(
        questionnaire_id=1, link_id="q1", annotation="あ", citations=[imported]
    )
    # UI は strip 済みの `unit_id` を送る（`_clean_citation` が strip する）
    [saved] = _normalize_citations(
        _item(unit_id=_FABRICATED_UNIT_ID, payload={"snippet": "抜粋"}), existing=existing
    )

    assert saved["payload"].get("origin") == ORIGIN_IMPORTED, (
        "空白つきで記録された取り込みの事実が引き継がれていない"
    )
    assert is_distributable_citation(saved), "取り込み由来の典拠が配布できなくなっている"


def test_clearing_the_unit_id_of_an_imported_citation_is_idempotent(index_dir: str) -> None:
    """**空になった典拠を書かない**こと。

    配布物から取り込んだ典拠は `{page, path, origin}` だけを持つ（`label` も `snippet` も無い）。
    利用者が典拠エディタで **`unit_id` の欄だけを空にして保存**すると、規則3 が許可リスト
    （`snippet` / `label`）で payload を払い落とすので、**`{unit_id: None, payload: None}` という
    空の典拠**が DB に書かれていた。次の保存では `_clean_citation` がそれを捨てるので
    `citations` が `[]` になる——**保存が冪等でない**。

    害は記録の来歴が壊れるときと同じである。`content_unchanged`
    （`row.citation_list == citations`）が偽になり、記述もアドレスも編集していない一括確認で
    **確認帰属が操作者へ移り、`updated_at` が進んで下流の手順書・プランが stale 化する**。
    空の典拠は `citations_total` にも数えられるので指標も動く。

    **`_clean_citation` の破棄規則が届かない経路**だったので、規則を `_nonempty_payload` に
    閉じ、`_verified_citation` の出口にも掛けた。
    """
    imported = {
        "unit_id": "file:別施設マニュアル.pdf#p3:0-100",
        "payload": {"page": 3, "path": "別施設マニュアル.pdf", "origin": ORIGIN_IMPORTED},
        "llm_model": None,
    }
    # 前提: 取り込み由来の payload は許可リストのキーを1つも持たない（持つと空にならない）
    assert not {"snippet", "label"} & set(imported["payload"]), "前提が崩れている"

    once = _normalize_citations(
        {"link_id": "q1", "citations": [{**imported, "unit_id": None}]},
    )
    assert once == [], "空の典拠が保存されている（次の保存で消えるので冪等でない）"

    # 抜粋を持つ典拠は `unit_id` を空にしても残る（「常に捨てる」実装を殺す）
    [kept] = _normalize_citations(
        {
            "link_id": "q1",
            "citations": [{**imported, "unit_id": None, "payload": {"snippet": "紙 p.7"}}],
        },
    )
    assert kept["payload"]["snippet"] == "紙 p.7"


def test_a_record_pointing_outside_the_index_is_never_cleaned_by_saving_again(
    index_dir: str,
) -> None:
    """**索引に無いアドレスの記録は、保存し直しても主張が残る**。

    これは規則2（記録を壊さない）の**意図した帰結**であり、欠陥ではない。ここで固定するのは
    **限界そのもの**である——索引に無いアドレスを指す汚染された記録は、何度保存し直しても
    配布物と典拠の付与率の分子に残り続ける。洗浄には既存レコードの一度きりの再照合が要る。

    想定する汚染の形は（`file:患者山田太郎のカルテ:0-0` に
    自己申告の `file_role: reference`）。**索引はロードできている**——参照不能でも索引の
    差し替えでもない、通常の環境である。

    再照合を入れて挙動が変われば**このテストが落ちる**ので、限界の記述の更新が強制される。
    """
    contaminated = {
        "unit_id": "file:患者山田太郎のカルテ:0-0",
        "payload": {"file_role": "reference", "path": "患者山田太郎のカルテ"},
    }
    existing = ClinicalAnnotation(
        questionnaire_id=1, link_id="q1", annotation="あ", citations=[dict(contaminated)]
    )
    # 前提: この記録は配布できてしまう状態にある（汚染そのもの）
    assert is_distributable_citation(contaminated), "前提: 汚染された記録は配布可能と判定される"
    # **前提: 索引はロードできている**。参照不能・差し替えの経路とは
    # 別の主張を測っているので、ロードが壊れたときに黙って「参照不能でも記録は残る」（別テストが
    # 既に持つ主張）へすり替わらないようにする
    probes = _normalize_citations(_item())
    assert probes, "前提: 索引はロードできている（典拠が破棄されている）"
    assert probes[0]["payload"] and probes[0]["payload"].get("page") == 12, (
        "前提: 索引はロードできている"
    )

    saved = [dict(contaminated)]
    for _ in range(3):
        [saved_one] = _normalize_citations(
            {"link_id": "q1", "citations": [dict(saved[0])]}, existing=existing
        )
        saved = [saved_one]
        assert is_distributable_citation(saved_one), (
            "保存し直すと洗浄されるかのように見える（限界の記述と実装が食い違っている）"
        )
        assert saved_one["payload"]["file_role"] == "reference"

    # **クライアントが payload を送らなくても記録から復元される**（利用者は消せない）
    [stripped] = _normalize_citations(
        {"link_id": "q1", "citations": [{"unit_id": contaminated["unit_id"], "payload": None}]},
        existing=existing,
    )
    assert stripped["payload"]["file_role"] == "reference", "記録からの復元が効いていない"
    assert is_distributable_citation(stripped)

    # **記録が無ければ主張は落ちる**（規則3。この限界は「記録がある場合だけ」である）
    [fresh] = _normalize_citations({"link_id": "q1", "citations": [dict(contaminated)]})
    assert not is_distributable_citation(fresh), "記録の無い新しい主張まで通っている"


def test_a_citation_in_the_middle_of_the_list_is_verified(index_dir: str) -> None:
    """**中間の位置も照合される**。

    既存のテストは `[索引にある, 捏造, 索引にある]` で、索引にあるユニットが**位置0と2**
    （どちらも偶数）にしか無かった。そのため「位置1だけ照合しない」（`if i != 1 and ...`）と
    「奇数位置を照合しない」（`i % 2 == 0`）の変異が素通りする。
    **鏡像**（捏造を両端・索引にあるものを中間）に置いて、奇数位置を測る。
    """
    indexed = {"unit_id": _UNIT_ID, "payload": {"file_role": "patient_data"}, "llm_model": None}
    fabricated = {
        "unit_id": _FABRICATED_UNIT_ID,
        "payload": {"file_role": "reference"},
        "llm_model": None,
    }
    first, middle, last = _normalize_citations(
        {"link_id": "q1", "citations": [dict(fabricated), dict(indexed), dict(fabricated)]}
    )

    # 中間が照合されている（照合されなければ自己申告の `patient_data` が残り配布不可になる）
    assert middle["payload"]["file_role"] == "reference", "中間の典拠が照合されていない"
    assert middle["payload"]["page"] == 12, "中間の典拠が照合されていない"
    assert is_distributable_citation(middle)
    # 両端は索引に無いので主張を落とす（＝照合そのものは全位置で走っている）
    for label, saved in (("先頭", first), ("末尾", last)):
        assert not is_distributable_citation(saved), f"{label}の捏造が配布できてしまう"
