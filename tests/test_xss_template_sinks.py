"""テンプレート / JS の XSS シンク（Claude Security F1〜F3, F5, F6, F10〜F12, F15, F19, F22, F24, F26）

設計者が決める文字列（Questionnaire の linkId・title、DSL の step id、データセット名）や
外部データ（FHIR Patient の氏名）が、ページ内の JS・HTML に**エスケープなしで**入る形が
同じクラスで11か所あった。個別に直しても、次に書くテンプレートが同じ型を繰り返す。
ここでは (1) 共通ヘルパーの仕様、(2) テンプレート・JS の静的な禁止パターン、
(3) 敵対的な文字列を実際に描画した結果、の3層で守る。

**静的検査の限界**: `innerHTML` に組み立てる文字列の中の `${...}` がエスケープ済みかどうかは
正規表現では決められない。そこは (3) の描画テストと、`escape.js` を使う規約で押さえる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import InputPlan
from kawasemi.models.dataset import Dataset
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.user import User

_WEB = Path(__file__).resolve().parents[1] / "kawasemi" / "web"
_TEMPLATES = _WEB / "templates"
_JS = _WEB / "static" / "js"

# 属性からも JS 文字列からも要素本文からも抜けようとする文字列。
PAYLOAD = "x'\"><img src=x onerror=alert(1)>"


def _template_files() -> list[Path]:
    return sorted(_TEMPLATES.rglob("*.html"))


# ---------------------------------------------------------------- (1) 共通ヘルパー


def test_the_shared_escape_helper_exists() -> None:
    """`kwEsc` の定義があること。**振る舞い**（5文字の変換・null・属性を抜けないこと）は
    jsdom で実際に動かす `tests/js/xss_handlers.test.js` が見る（`test_js_handlers.py`）。
    ここで文字列の部分一致を見ても、変換の漏れは検出できない。"""
    assert "function kwEsc" in (_JS / "escape.js").read_text(encoding="utf-8")


def test_base_template_loads_the_escape_helper() -> None:
    base = (_TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "js/escape.js" in base


@pytest.mark.parametrize(
    "path", _template_files(), ids=lambda p: p.relative_to(_TEMPLATES).as_posix()
)
def test_templates_that_use_the_helper_inherit_the_base(path: Path) -> None:
    """`kwEsc` を呼ぶページは base.html を継承していること（読み込まれないと実行時エラー）。"""
    text = path.read_text(encoding="utf-8")
    if "kwEsc(" in text:
        assert '{% extends "base.html" %}' in text


# ------------------------------------------------------- (2) 静的な禁止パターン


@pytest.mark.parametrize(
    "path", _template_files(), ids=lambda p: p.relative_to(_TEMPLATES).as_posix()
)
def test_no_jinja_value_inside_an_inline_event_handler(path: Path) -> None:
    """`onclick="f('{{ x }}')"` を書かない。

    HTML のエスケープは**属性をパースした後**に外れるので、`'` が JS 文字列の終端になる
    （Jinja の既定の `&#39;` も、`| e` も効かない）。値は `data-*` 属性に置き、
    `addEventListener` で読む。
    """
    text = path.read_text(encoding="utf-8")
    bad = re.findall(r"""\bon[a-z]+\s*=\s*(?:"[^"]*\{\{[^"]*"|'[^']*\{\{[^']*')""", text)
    assert not bad, f"インラインのイベントハンドラに Jinja の値が入っている: {bad}"


@pytest.mark.parametrize(
    "path", _template_files(), ids=lambda p: p.relative_to(_TEMPLATES).as_posix()
)
def test_no_safe_filter_or_markup_bypass(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"\|\s*safe\b", text), "|safe でエスケープを外している"


def test_no_script_builds_inline_handlers_from_values() -> None:
    """JS から `setAttribute('onclick', …値…)` を組み立てない。"""
    files = [*_template_files(), *_JS.glob("*.js")]
    for path in files:
        if path.name == "mermaid.min.js":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"setAttribute\(\s*['\"]on[a-z]+['\"]", text), path.name


@pytest.mark.parametrize(
    "path",
    [*_template_files(), *[p for p in _JS.glob("*.js") if p.name != "mermaid.min.js"]],
    ids=lambda p: p.name,
)
def test_no_textcontent_to_innerhtml_escape(path: Path) -> None:
    """`div.textContent = s; return div.innerHTML` 型の esc() を書かない。

    これは `"` と `'` をエスケープしないので、`value="${esc(x)}"` に入れると属性を抜けられる。
    """
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"textContent\s*=\s*[^;]*;\s*return\s+\w+\.innerHTML", text), path.name


# ----------------------------------------------------------- (3) 敵対的な描画


def _assert_payload_is_inert(html: str) -> None:
    """ペイロードが生のまま HTML に出ていない。"""
    assert "<img src=x onerror=alert(1)>" not in html
    # イベントハンドラ属性の中に入っていない
    assert not re.search(r"""\bon[a-z]+\s*=\s*["'][^"']*onerror""", html)


async def _questionnaire(db: AsyncSession, *, title: str = PAYLOAD, link_id: str = PAYLOAD):
    q = Questionnaire(
        fhir_id="q-xss",
        title=title,
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-xss",
            "title": title,
            "status": "active",
            "item": [{"linkId": link_id, "text": PAYLOAD, "type": "string"}],
        },
    )
    db.add(q)
    await db.flush()
    return q


@pytest.mark.asyncio
async def test_questionnaire_list_title_does_not_reach_inline_js(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """F2: 質問票タイトルは onclick の JS 文字列に入らず、data 属性に入る。"""
    await _questionnaire(db_session)
    await db_session.commit()
    resp = await admin_client.get("/questionnaires")
    assert resp.status_code == 200
    _assert_payload_is_inert(resp.text)
    assert "data-delete-title=" in resp.text
    assert "confirmDelete(" not in re.sub(r"<script.*?</script>", "", resp.text, flags=re.S)


@pytest.mark.asyncio
async def test_dataset_list_name_does_not_reach_inline_js(
    admin_client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """F12: データセット名は onsubmit の JS 文字列に入らず、data 属性に入る。"""
    db_session.add(Dataset(name=PAYLOAD, created_by_id=admin_user.id))
    await db_session.commit()
    resp = await admin_client.get("/datasets")
    assert resp.status_code == 200
    _assert_payload_is_inert(resp.text)
    assert "data-confirm-name=" in resp.text
    assert not re.search(r"onsubmit=", resp.text)


@pytest.mark.asyncio
async def test_autofill_page_link_id_does_not_reach_inline_js(
    designer_client: AsyncClient, db_session: AsyncSession
) -> None:
    """F1/F3/F10/F11: linkId は onclick の JS 文字列にも innerHTML 用の JS にも生では入らない。"""
    q = await _questionnaire(db_session)
    db_session.add(
        InputPlan(
            questionnaire_id=q.id,
            link_id=PAYLOAD,
            item_text="t",
            program={
                "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}],
                "return": "$s1",
            },
            status="confirmed",
        )
    )
    await db_session.commit()
    resp = await designer_client.get(f"/ai-input/questionnaires/{q.id}/autofill")
    assert resp.status_code == 200
    _assert_payload_is_inert(resp.text)
    assert "data-link-id=" in resp.text
    assert "highlightEvidence('" not in resp.text


@pytest.mark.asyncio
async def test_design_system_page_link_id_does_not_reach_inline_js(
    designer_client: AsyncClient, db_session: AsyncSession
) -> None:
    """F15: データ所在の画面のボタンも linkId を JS 文字列に埋め込まない。"""
    q = await _questionnaire(db_session)
    await db_session.commit()
    resp = await designer_client.get(f"/ai-input/questionnaires/{q.id}/source-survey/system")
    assert resp.status_code == 200
    _assert_payload_is_inert(resp.text)
    assert "confirmItem(this, '" not in resp.text
    assert "aiDraftItem(this, '" not in resp.text


@pytest.mark.asyncio
async def test_plan_and_runbook_pages_embed_data_as_inert_json(
    designer_client: AsyncClient, db_session: AsyncSession
) -> None:
    """F5/F6/F24/F26: 手順・プランの画面データは `tojson`（`<` `>` `'` を \\u エスケープ）で入る。

    クライアントで組み立てる HTML 側のエスケープ（`esc()` → `kwEsc`）はここでは見ない。
    静的検査（`textContent→innerHTML` 型の禁止）と jsdom の振る舞いテストで守る。
    """
    q = await _questionnaire(db_session)
    await db_session.commit()
    for page in ("plan", "runbook"):
        resp = await designer_client.get(f"/ai-input/questionnaires/{q.id}/{page}")
        assert resp.status_code == 200, page
        assert "<img src=x onerror=alert(1)>" not in resp.text, page
