"""人が典拠を選ぶための索引引きのテスト

この経路の要点は3つで、いずれも**測らないと黙って壊れる**。

1. **LLM も embedding も呼ばない。** 人が典拠を探すたびにモデルが動くと、人の作業と
   比較用の自動生成版との独立を運用の注意だけで守ることになる。呼ばないことを構造で固定する
2. **候補の母集合が AI 経路と同じ**（`is_citable_reference_unit`）。人だけが目次チャンクを
   典拠にできると、人が書いた版と自動生成版の典拠を突き合わせる比較で候補集合が食い違う
3. **選んだ典拠が保存経路を通る。** 索引に実在するアドレスなので `_verified_citation`
   が索引の payload を採り、`is_distributable_citation` を通る
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import routes as routes_mod
from kawasemi.ai_input.reference_lookup import (
    MAX_LIMIT,
    find_reference_units,
    reference_lookup_available,
    reference_page_range,
)
from kawasemi.ai_input.resource_index.schema import SCHEMA_VERSION
from kawasemi.ai_input.resource_index.store import reset_store_cache
from kawasemi.ai_input.routes import _normalize_citations
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from kawasemi.utils.file_export import is_distributable_citation

_REPO_ROOT = Path(__file__).resolve().parents[2]
# **画面の JS は静的ファイルへ切り出した**。テンプレートには設定と
# `<script src>` しか残らないので、配線の検査はこのファイルに対して行う。
_UI_TEMPLATE = _REPO_ROOT / "kawasemi/web/templates/ai_input/design_clinical.html"
_UI_JS = _REPO_ROOT / "kawasemi/web/static/js/design_clinical.js"

# 原典の目次は「項目名 ·········· ページ番号」の形で組まれる（`is_index_of_contents_unit`）。
_TOC_TEXT = "[5] 感染症の発症日 ···························································· 4"


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    reset_store_cache()
    yield
    reset_store_cache()


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


def _unit(
    *, page: int, start: int, end: int, text: str, file_role: str = "reference"
) -> dict[str, Any]:
    return {
        "unit_id": f"file:manual.pdf#p{page}:{start}-{end}",
        "kind": "file",
        "label": f"manual.pdf（p{page}）",
        "search_text": text,
        "snippet": text[:40],
        "payload": {
            "file_role": file_role,
            "path": "manual.pdf",
            "page": page,
            "char_range": [start, end],
            "mime": "application/pdf",
        },
    }


# 同一ページが複数チャンクに割れ、範囲が重なる形（大きな文書で起きうる形）。
_UNITS = [
    _unit(
        page=12, start=700, end=1500, text="[5] 感染症の発症日は、感染症の症状が最初に出現した日"
    ),
    _unit(page=12, start=0, end=800, text="診断日は、感染症の診断が確定した日とする。"),
    _unit(page=3, start=0, end=800, text=_TOC_TEXT),
    _unit(page=27, start=0, end=800, text="起因微生物は、培養で分離された菌名を記録する。"),
    _unit(page=41, start=0, end=800, text="患者の氏名は登録しない", file_role="patient_data"),
]


def _write_index(base: Path, units: list[dict[str, Any]]) -> str:
    base.mkdir(parents=True, exist_ok=True)
    (base / "build_info.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "indexer_version": "0.1.0",
                "embedding_model": "bge-m3",
                "dim": 4,
                "normalized": True,
                "unit_count": len(units),
                "kind_counts": {"file": len(units)},
                "built_at": "2026-08-15T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (base / "file_index.jsonl").write_text(
        "".join(json.dumps(u, ensure_ascii=False) + "\n" for u in units), encoding="utf-8"
    )
    np.save(base / "embeddings.npy", np.ones((len(units), 4), dtype=np.float32))
    return str(base)


@pytest.fixture
def index_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = _write_index(tmp_path / "resource_index", _UNITS)
    monkeypatch.setattr(settings, "resource_index_dir", path)
    return path


def test_the_candidates_are_the_citable_reference_units(index_dir: str) -> None:
    """母集合は**規約文書のうち目次を除いたもの**（AI 経路と同じ述語）。"""
    hits, total = find_reference_units()

    unit_ids = [h.unit_id for h in hits]
    assert total == 3, f"目次と patient_data を除いた3件のはず: {unit_ids}"
    assert "file:manual.pdf#p3:0-800" not in unit_ids, "目次チャンクが候補に出ている"
    assert "file:manual.pdf#p41:0-800" not in unit_ids, "患者データのユニットが候補に出ている"


def test_the_hits_are_ordered_by_page_then_position(index_dir: str) -> None:
    """並びはページ順・ページ内は原文の順。

    同順の並びが実行ごとに変わると、人は「さっき見た候補」を追えない。
    """
    hits, _ = find_reference_units()

    assert [h.unit_id for h in hits] == [
        "file:manual.pdf#p12:0-800",
        "file:manual.pdf#p12:700-1500",
        "file:manual.pdf#p27:0-800",
    ]


def test_the_page_filter_narrows_to_one_page(index_dir: str) -> None:
    hits, total = find_reference_units(page=12)

    assert total == 2
    assert {h.page for h in hits} == {12}


def test_the_query_matches_a_substring_of_the_original_text(index_dir: str) -> None:
    """項目番号（原文に `[5]` の形で現れる）で引けること。"""
    hits, total = find_reference_units(query="[5]")

    assert total == 1
    assert hits[0].unit_id == "file:manual.pdf#p12:700-1500"


def test_the_query_and_the_page_are_combined(index_dir: str) -> None:
    _, total = find_reference_units(page=27, query="[5]")

    assert total == 0, "ページと語は AND で効く"


def test_the_total_counts_before_the_limit(index_dir: str) -> None:
    """**絞る前の総数を返す。** 上限で切ったことが分からないと、人はそれを
    「マニュアルに該当箇所が無い」（意味が決まらなかった項目）と読み違える。
    """
    hits, total = find_reference_units(limit=1)

    assert len(hits) == 1
    assert total == 3


def test_the_limit_is_capped(index_dir: str) -> None:
    """上限を超える要求は丸める（1リクエストで索引を吸い出させない）。"""
    hits, _ = find_reference_units(limit=MAX_LIMIT + 1000)

    assert len(hits) <= MAX_LIMIT


def test_the_source_is_built_by_the_shared_formatter(index_dir: str) -> None:
    """出典表記は配布物と同じ `format_citation_source` を通る（画面が組み立て直さない）。"""
    hits, _ = find_reference_units(page=27)

    assert hits[0].source == "規約文書: manual.pdf p.27（0-800）"


def test_the_page_range_is_reported(index_dir: str) -> None:
    assert reference_page_range() == (12, 27), "目次と patient_data のページは範囲に入らない"


def test_a_missing_index_degrades_to_no_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引が無い環境でも例外にしない（候補ゼロへ縮退する）。"""
    monkeypatch.setattr(settings, "resource_index_dir", str(tmp_path / "absent"))

    assert reference_lookup_available() is False
    assert find_reference_units() == ([], 0)
    assert reference_page_range() is None


def test_the_lookup_does_not_use_embeddings_or_an_llm(
    index_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**モデルを1回も呼ばない。**

    呼ばないことを「呼んだら落ちる」形で固定する。ここが embedding 検索に化けても
    結果の見た目は似るので、出力の検査では気づけない。
    """
    import kawasemi.ai_input.llm_json as llm_json
    from kawasemi.ai_input.resource_index.embedder import OllamaEmbedder
    from kawasemi.ai_input.resource_index.store import ResourceIndexStore

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("典拠の索引引きがモデルを呼んだ")

    # `raising=True`（既定）のままにする。**属性名が変わったらテストが落ちる**ようにしないと、
    # 監視対象が消えたことに気づかないまま「呼ばない」を主張し続けることになる。
    monkeypatch.setattr(OllamaEmbedder, "embed", _explode)
    monkeypatch.setattr(ResourceIndexStore, "search", _explode)
    monkeypatch.setattr(llm_json, "chat_json", _explode)

    hits, total = find_reference_units(page=12, query="診断日")

    assert total == 1 and hits[0].page == 12


@pytest.mark.asyncio
async def test_the_endpoint_returns_the_candidates_with_the_total(
    designer_client: AsyncClient, designer_user: User, index_dir: str
) -> None:
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.get("/ai-input/reference-units", params={"page": 12})
    body = resp.json()

    assert resp.status_code == 200
    assert body["success"] is True
    assert body["total"] == 2
    assert body["shown"] == 2
    assert body["page_range"] == [12, 27]
    assert {u["unit_id"] for u in body["units"]} == {
        "file:manual.pdf#p12:0-800",
        "file:manual.pdf#p12:700-1500",
    }


@pytest.mark.asyncio
async def test_the_endpoint_reports_an_unavailable_index_as_a_failure(
    designer_client: AsyncClient,
    designer_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引が無いことを成功として返さない（AI下書きの経路と同じ扱い）。

    空配列を 200 で返すと、画面は「マニュアルに該当箇所が無い」と読める表示になる。
    索引の不在が「意味が決まらなかった項目」の件数へ流れ込む。
    """
    monkeypatch.setattr(settings, "resource_index_dir", str(tmp_path / "absent"))
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.get("/ai-input/reference-units")

    assert resp.status_code == 409
    assert resp.json()["index_unavailable"] is True


@pytest.mark.asyncio
async def test_the_endpoint_requires_the_designer_role(
    designer_client: AsyncClient, collector_user: User, index_dir: str
) -> None:
    """権限は臨床的意味の画面と同じ（設計者）。索引の中身は施設のスキーマ情報を含みうる。"""
    _act_as(designer_client, collector_user, RoleType.COLLECTOR)

    resp = await designer_client.get("/ai-input/reference-units")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_the_screen_offers_the_picker_without_ai_drafting(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    index_dir: str,
) -> None:
    """**AI下書きが無効でも典拠を選べる**（このテストの眼目）。

    `ENABLE_AI_DRAFTING=false` は LLM を置かない施設のサポート済みの構成で、そこでも
    人は典拠を付けられなければならない。
    """
    # 画面のフラグは**テンプレートのグローバル**（import 時に settings から焼かれる）なので、
    # `settings` を差し替えても描画は変わらない。描画側を直接差し替える。
    monkeypatch.setitem(routes_mod.templates.env.globals, "enable_ai_drafting", False)
    q = Questionnaire(
        fhir_id="q-reference-picker",
        title="典拠ピッカーのテスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-reference-picker",
            "item": [{"linkId": "item1", "text": "診断日", "type": "string"}],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.get(f"/ai-input/questionnaires/{q.id}/source-survey/clinical")
    # JS は静的ファイルへ切り出してある。ブラウザが受け取る合計を見る。
    body = resp.text + _UI_JS.read_text(encoding="utf-8")

    assert resp.status_code == 200
    assert "AI下書き（規約文書から）" not in body, "前提: AI下書きは無効"
    assert "索引から選ぶ" in body
    assert "/ai-input/reference-units?" in body


def test_the_picker_drops_the_ai_provenance_of_the_row() -> None:
    """**選び直した行から AI 由来の印を落とすこと。**

    典拠エディタの1行は2つの印を `dataset` に持つ。

    | 印 | 読む側 | 残ると何が起きるか |
    |---|---|---|
    | `llmModel` | `collectCitations` が `llm_model` として保存に載せる | **人が選んだ典拠が AI 由来として記録される**（人が書いた版と自動生成版の典拠を突き合わせる比較で区別が付かない） |
    | `aiDraft` | `applyDraft` が `.cite-row[data-ai-draft="1"]` を remove する | **次にその項目のAI下書きを引き直した時点で、人が選んだ典拠だけが黙って消える** |

    どちらも「AI下書き → 典拠が違う → 索引から選び直す」という自然な操作で踏む。
    `applyPickedUnit` は `cite-meta` を出典表記で上書きするので、`llmModel` が残っていても
    画面からは「AI案: …」の表示が消える——**人には見えない形で来歴だけがずれる。**

    **これは文字列検査であり、JS の実行は見ていない**（画面の JS を
    走らせる仕組みがリポジトリに無い）。捕まえられるのはこの2行が消えたときだけで、
    `dataset` の読み側が別のキー名へ変わった場合は素通りする。`_normalize_citations` 側の
    不変条件は `test_a_picked_unit_survives_the_save_path` が別に固定している。
    """
    source = _UI_JS.read_text(encoding="utf-8")
    start = source.index("function applyPickedUnit(")
    body = source[start : source.index("\n}\n", start)]  # 閉じ括弧（桁0）までがこの関数

    assert "row.dataset.llmModel = ''" in body, "AI の来歴が人手の典拠に引き継がれる"
    assert "delete row.dataset.aiDraft" in body, "次のAI下書きで人手の典拠が消える"


def test_a_picked_unit_survives_the_save_path(index_dir: str) -> None:
    """選んだ候補が**保存経路を通る**（索引照合 → 配布可否）。

    人が選んだ典拠は AI 由来のものと同じ位置に着地する必要がある。ここが崩れると、
    「AI下書きの典拠だけが分子に入る」という以前の状態へ戻る。
    """
    [hit] = find_reference_units(page=27)[0]
    # 画面が送る形（payload は表示用の出典表記だけ。索引由来の値はサーバが採り直す）
    item = {
        "link_id": "q1",
        "citations": [{"unit_id": hit.unit_id, "payload": {"label": hit.source}}],
    }

    [saved] = _normalize_citations(item)

    assert saved["payload"]["path"] == "manual.pdf"
    assert saved["payload"]["page"] == 27, "索引の型（int）のまま採る"
    assert saved["payload"]["file_role"] == "reference"
    assert saved["llm_model"] is None, "人手で選んだ典拠に AI の来歴を付けない"
    assert is_distributable_citation(saved), "配布可能な典拠として成立していない"
