"""設計ロールの LLM 来歴の収集・保存とガードのテスト

このテーブルの前提は「設計ロールの入力に患者データが含まれない」ことなので、
**ガードが効いていること**と、同じくらい重要な**正常系で発火しないこと**を固定する。

1. 役割の許可リスト（実行ロールの来歴が入る経路を閉じる）。入口と保存時の両方
2. 内容由来テキストの出所（`content_sources` への記録と、索引の不変条件検査）
3. 保存上限での切り詰めと、切り詰めた事実の記録

**ガード2 は2度作り直している。** 初版は「プロンプト全文から `{patient_id}` などの
マーカーを探す」実装で、DSL の `{{patient_id}}` にも一致して正常系を落としていた。
2版目は「非空 snippet を持つユニットはすべて中身」と扱ったが、`build_manifest_units`
が作る**所在宣言**ユニットは非空 snippet（`path_pattern`）を持つため、患者データが所在
候補に入るという正常系でやはり必ず発火していた。どちらも「正常系で発火し、本来の脅威
では発火しない」という同じ性質だった。

そこで現行版は `char_range` の有無で「中身」と「所在宣言」を構造的に分ける。
**手作りの `ResourceUnit` ではこの欠陥を再現できない**ので、`build_manifest_units` /
`build_reference_units` の実出力を通す正常系テストを置いてある。
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kawasemi_indexer.builder import build_index
from kawasemi_indexer.file_indexer import build_manifest_units, build_reference_units
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import resource_drafter as rd_mod
from kawasemi.ai_input.design_provenance import (
    MAX_FIELD_CHARS,
    DesignLlmRecorder,
    DesignProvenanceError,
    save_design_llm_calls,
    save_design_llm_calls_safely,
)
from kawasemi.ai_input.llm_json import LLMCallRecord
from kawasemi.ai_input.models import DesignLlmCall, DesignRole
from kawasemi.ai_input.resource_drafter import AIResourceDrafter
from kawasemi.ai_input.resource_index.schema import Kind, ResourceUnit
from kawasemi.ai_input.resource_index.store import reset_store_cache
from kawasemi.models.questionnaire import Questionnaire
from tests.ai_input.conftest import HashingEmbedder


def _record(**overrides: object) -> LLMCallRecord:
    base = {
        "label": "手順書生成",
        "model": "model-c:9b",
        "options": {"temperature": 0, "num_ctx": 8192},
        "system_prompt": "あなたはEDCのアシスタントです。",
        "user_message": "### linkId: item1\n  項目名: 診断日\n",
        "raw_output": '{"runbooks": []}',
    }
    base.update(overrides)
    return LLMCallRecord(**base)  # type: ignore[arg-type]


async def _questionnaire(db: AsyncSession) -> int:
    q = Questionnaire(
        fhir_id="q-provenance",
        title="来歴テスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-provenance"},
    )
    db.add(q)
    await db.flush()
    return int(q.id)


# --------------------------------------------------------------------------- #
# ガード1: 役割の許可リスト
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", sorted(DesignRole.ALL))
def test_recorder_accepts_design_roles(role: str) -> None:
    assert DesignLlmRecorder(role).role == role


@pytest.mark.parametrize("role", ["extraction", "llm_extract", "", "runbook2"])
def test_recorder_rejects_non_design_roles(role: str) -> None:
    """実行ロールの来歴がこのテーブルへ入る経路を構造的に閉じること。

    実行ロール（llm_extract / llm_judge）は患者データを読むので、来歴は evidence trace
    側に閉じる。ここへ流れ込むと PHI 非該当の前提が崩れる。
    """
    with pytest.raises(DesignProvenanceError):
        DesignLlmRecorder(role)


def _listing_client(model: str, digest: str) -> SimpleNamespace:
    """`list()` を持つ client（Ollama 相当）を作る。ダイジェストの出所は `/api/tags`。"""

    async def _list() -> SimpleNamespace:
        return SimpleNamespace(models=[SimpleNamespace(model=model, digest=digest)])

    return SimpleNamespace(list=_list)


@pytest.mark.asyncio
async def test_save_revalidates_role_after_reassignment(db_session: AsyncSession) -> None:
    """入口を通ったあとに role を差し替えても保存時に拒否されること（ガード1の独立性）。

    入口（`__init__`）の検査だけだと `recorder.role = "llm_extract"` の再代入で
    実行ロールの来歴を入れられる。層として独立させるため保存時にも検査する。

    **補助関数（`_assert_design_role`）を直接叩くのでは検証にならない。** 保存経路が
    その検査を呼んでいることこそが要点なので、`save_design_llm_calls` を実際に呼び、
    例外が出ることと**行が1件も保存されていないこと**を固定する。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.RUNBOOK)
    recorder.role = "llm_extract"
    recorder(_record())

    with pytest.raises(DesignProvenanceError, match="設計ロール以外"):
        await save_design_llm_calls(
            db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
        )

    rows = (await db_session.execute(select(DesignLlmCall))).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- #
# ガード2: 内容由来テキストの出所の検査
#
# 中身つきユニット（`build_reference_units` 由来）と所在宣言ユニット
# （`build_manifest_units` 由来）を payload の `char_range` の有無で区別する。
# 前者だけが原資料の中身であり、後者は索引作成者が書いたメタ情報である。
# --------------------------------------------------------------------------- #


def _content_unit(kind: str, *, file_role: str | None) -> ResourceUnit:
    """中身をチャンク化したユニット（`char_range` を持つ）を模す。"""
    payload: dict[str, object] = {}
    if kind == Kind.FILE:
        payload["path"] = "protocol.md"
        payload["char_range"] = [0, 200]
    if file_role is not None:
        payload["file_role"] = file_role
    return ResourceUnit(
        unit_id=f"{kind}:{file_role}:u1",
        kind=kind,
        label="ユニット",
        search_text="診断日",
        snippet="本文の抜粋",
        payload=payload,
    )


def _manifest_unit(file_role: str = "patient_data") -> ResourceUnit:
    """所在宣言ユニット（中身を索引せず `char_range` を持たない）を模す。"""
    return ResourceUnit(
        unit_id=f"file:{file_role}:病理レポート",
        kind=Kind.FILE,
        label="病理レポート",
        search_text="病理レポート",
        # snippet は非空だが中身ではなく path_pattern（＋notes）である
        snippet="{{patient_id}}/pathology.txt — 患者ごとの病理診断レポート",
        payload={
            "file_role": file_role,
            "path_pattern": "{{patient_id}}/pathology.txt",
            "extract": {"method": "text"},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("file_role", ["patient_data", "unknown", None])
async def test_save_rejects_non_reference_file_content(
    db_session: AsyncSession, file_role: str | None
) -> None:
    """**中身つき**ファイルユニットの file_role が reference 以外なら保存しないこと。

    これは索引の不変条件検査である。`build_reference_units` は `char_range` と
    `file_role=reference` を必ず同時に付けるので、正しい索引ではこの組み合わせは
    成立しない。成り立つのは索引ビルダかプロンプト構築が壊れたときだけである。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    recorder.declare_content_sources([_content_unit(Kind.FILE, file_role=file_role)])
    recorder(_record())

    with pytest.raises(DesignProvenanceError, match="file_role=reference 以外"):
        await save_design_llm_calls(
            db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
        )

    rows = (await db_session.execute(select(DesignLlmCall))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_save_rejects_whole_batch_on_single_detection(db_session: AsyncSession) -> None:
    """1件でも混ざればバッチ全体を保存しないこと。

    一部だけ残すと、後から「何が保存されたか」を判断できない。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.SYSTEM)
    recorder.declare_content_sources(
        [
            _content_unit(Kind.FILE, file_role="reference"),  # 健全
            _content_unit(Kind.DB, file_role=None),  # 健全（スキーマ定義）
            _content_unit(Kind.FILE, file_role="patient_data"),  # 索引が壊れている
        ]
    )
    recorder(_record())
    recorder(_record(chunk_index=1))

    with pytest.raises(DesignProvenanceError):
        await save_design_llm_calls(
            db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
        )

    rows = (await db_session.execute(select(DesignLlmCall))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_save_allows_patient_data_location_declaration(db_session: AsyncSession) -> None:
    """患者データの**所在宣言**が候補に入っても発火せず、来歴が保存されること。

    患者データファイルは所在候補として正当なので、データ所在ロールの候補に入るのは
    正常系である。所在宣言ユニットは非空 snippet（`path_pattern`）を持つため、
    「snippet があれば中身」と扱う実装ではここで必ず発火し、`system` ロールの来歴が
    `logger.error` 以外の痕跡なく失われていた。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.SYSTEM)
    recorder.declare_content_sources(
        [_manifest_unit(), _content_unit(Kind.FILE, file_role="reference")]
    )
    recorder(_record(user_message="候補: {{patient_id}}/pathology.txt — 病理レポート"))

    saved = await save_design_llm_calls(
        db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
    )
    assert saved == 1

    row = (await db_session.execute(select(DesignLlmCall))).scalars().one()
    # 所在宣言は中身ではないので出所に入らない（reference の中身だけが残る）
    assert row.content_sources == [f"{Kind.FILE}:reference"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "units",
    [
        [],  # 索引の中身を読まないロール（取得手順書・取得プラン）
        [(Kind.FILE, "reference")],  # 規約文書の抜粋
        [(Kind.DB, None), (Kind.FHIR, None), (Kind.MASTER, None)],  # 定義・コード表
    ],
)
async def test_save_allows_definitions_and_reference_content(
    db_session: AsyncSession, units: list[tuple[str, str | None]]
) -> None:
    """定義系ユニットと reference の中身は保存を妨げず、出所が行に残ること。

    ここが通らないと、`{patient_id}` を含む正当な所在候補（patient_data の
    `path_pattern`）や DSL の `{{patient_id}}` で正常系が止まる — 置き換え前の実装が
    まさにそれだった。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.SYSTEM)
    recorder.declare_content_sources([_content_unit(k, file_role=r) for k, r in units])
    # 正当な所在候補の snippet（path_pattern）と DSL のプレースホルダを本文に含める
    recorder(
        _record(
            user_message="候補: {patient_id}/pathology.txt — 病理レポート\n"
            'SQL: SELECT v FROM t WHERE id = "{{patient_id}}"'
        )
    )

    saved = await save_design_llm_calls(
        db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
    )
    assert saved == 1

    row = (await db_session.execute(select(DesignLlmCall))).scalars().one()
    expected = sorted(f"{k}:{r}" if r else k for k, r in units)
    assert row.content_sources == expected


# --------------------------------------------------------------------------- #
# ガード2: 実索引ビルダ（indexer の `kawasemi_indexer.builder`）の出力を通した正常系
#
# 手作りの ResourceUnit では「所在宣言ユニットが非空 snippet を持つ」という索引の
# 実際の作り方を再現できず、正常系でガードが発火する欠陥を見逃す。ここは
# `build_manifest_units` / `build_reference_units` の実出力をそのまま通す。
# --------------------------------------------------------------------------- #

_DEMO_MANIFEST = [
    {
        "label": "病理診断レポート",
        "aliases": ["病理レポート"],
        "path_pattern": "{{patient_id}}/pathology.txt",
        "extract": {"method": "text"},
        "connection_id": "report_fs",
        "notes": "患者ごとの病理診断レポート本文",
    }
]


def test_manifest_units_carry_non_empty_snippet_without_content() -> None:
    """索引の契約の確認: 所在宣言ユニットは非空 snippet を持つが中身ではない。

    `snippet` には `path_pattern`（＋notes）が入る。所在宣言なので `char_range` は
    持たない。この2点が `declare_content_sources` の絞り込みの根拠である。
    """
    unit = build_manifest_units(_DEMO_MANIFEST)[0]
    assert unit.snippet  # 非空（＝「snippet があれば中身」とは言えない）
    assert "char_range" not in unit.payload
    assert unit.payload["file_role"] == "patient_data"
    assert unit.payload["path_pattern"] == "{{patient_id}}/pathology.txt"


def test_manifest_units_are_not_declared_as_content_sources() -> None:
    """所在宣言ユニットは「中身の出所」ではないので宣言しないこと。

    患者データファイルは**所在候補として正当**なので候補に入るのは正常系である。
    これを中身として宣言すると、ガード2 が正常系で必ず発火し、データ所在ロールの
    来歴が `logger.error` 以外の痕跡なく失われる。
    """
    recorder = DesignLlmRecorder(DesignRole.SYSTEM)
    recorder.declare_content_sources(build_manifest_units(_DEMO_MANIFEST))
    assert recorder.content_sources == set()


def test_reference_units_are_declared_as_content_sources(tmp_path: Path) -> None:
    """中身をチャンク化した reference ユニットは宣言すること（`char_range` を持つ）。"""
    (tmp_path / "protocol.md").write_text("本試験の主要評価項目は全生存期間である。", "utf-8")
    units = build_reference_units(tmp_path, connection_id="docs_fs")
    assert units and all("char_range" in u.payload for u in units)

    recorder = DesignLlmRecorder(DesignRole.SYSTEM)
    recorder.declare_content_sources(units)
    assert recorder.content_sources == {f"{Kind.FILE}:reference"}


@pytest.mark.asyncio
async def test_drafter_over_real_index_saves_provenance(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """実索引（reference チャンク＋患者データ所在宣言）で来歴が保存されること。

    `AIResourceDrafter` は snippet を持つ全 hit を宣言対象へ渡すので、患者データの
    所在宣言ユニットが候補に入った瞬間にガード2 が発火する実装だと、ここで
    `save_design_llm_calls` が `DesignProvenanceError` を投げる（＝データ所在ロールの
    来歴が保存されない）。
    """
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    (ref_dir / "manual.md").write_text("組織型は病理診断報告書の記載に従って入力する。", "utf-8")
    manifest = tmp_path / "file_manifest.jsonl"
    manifest.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in _DEMO_MANIFEST), "utf-8"
    )
    index_dir = tmp_path / "resource_index"
    build_index(
        index_dir,
        HashingEmbedder(),
        file_source=ref_dir,
        file_manifest=manifest,
    )

    reset_store_cache()
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=str(index_dir))
    drafter._embedding_model = HashingEmbedder().model

    async def fake_chat(**kwargs: object) -> types.SimpleNamespace:
        # 所在提案は**2種類のコールを打つ**（検索ヒント→選択）。ヒントには
        # 空の `hints` で答え、施策抜きで縮退させる（この版より前と同じ検索になる）。
        fmt = kwargs.get("format") or {}
        if "hints" in fmt.get("properties", {}):  # type: ignore[union-attr]
            payload: dict = {"hints": []}
        else:
            payload = {"choices": [{"link_id": "item1", "chosen_cand_id": 0}]}
        return types.SimpleNamespace(
            message=types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        )

    drafter._client = types.SimpleNamespace(chat=fake_chat)

    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.SYSTEM)
    result = await drafter.draft(
        [{"link_id": "item1", "item_text": "組織型", "annotation": None}], recorder=recorder
    )
    drafts = result.items
    # 患者データの所在宣言ユニットが候補に入っている（正常系であることの確認）
    assert any(
        c.payload.get("file_role") == "patient_data" for d in drafts for c in d.candidates
    ), "所在宣言ユニットが候補に入らないと、この欠陥の正常系を再現できない"

    saved = await save_design_llm_calls(
        db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
    )
    # 検索ヒントと選択の2本。どちらも同じ recorder に載り、まとめて保存される。
    assert saved == 2

    rows = (await db_session.execute(select(DesignLlmCall))).scalars().all()
    assert [r.label for r in rows] == ["所在検索ヒント", "データ所在AI案"]
    # 候補を載せるのは選択のコールなので、プロンプトの中身はそちらを見る。
    row = rows[-1]
    assert row.role == DesignRole.SYSTEM
    # **所在の候補から reference を外したので、宣言する中身が無くなった。**
    # 以前は「中身を読んだのは reference のチャンクだけ」だったが、この役割はもう規約文書を
    # 読まない。所在宣言（patient_data）は中身ではないので、こちらも出所には入らない。
    assert row.content_sources == []
    # 所在候補としての path_pattern はプロンプトに現れている（正常系の綴り）
    assert "{{patient_id}}/pathology.txt" in row.user_message


# --------------------------------------------------------------------------- #
# 保存内容
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_persists_prompt_and_raw_output(db_session: AsyncSession) -> None:
    """プロンプト全文と生応答が保存されること（実行ロールの evidence と同じ粒度）。"""
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.RUNBOOK)
    recorder(_record(chunk_index=0, item_count=4))
    recorder(_record(chunk_index=1, item_count=2))

    saved = await save_design_llm_calls(
        db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
    )
    assert saved == 2

    rows = (
        (await db_session.execute(select(DesignLlmCall).order_by(DesignLlmCall.chunk_index)))
        .scalars()
        .all()
    )
    assert [r.chunk_index for r in rows] == [0, 1]
    assert [r.item_count for r in rows] == [4, 2]
    assert rows[0].role == DesignRole.RUNBOOK
    assert rows[0].system_prompt.startswith("あなたは")
    assert "診断日" in rows[0].user_message
    assert rows[0].raw_output == '{"runbooks": []}'
    assert rows[0].options == {"temperature": 0, "num_ctx": 8192}
    assert rows[0].truncated is False
    assert rows[0].error is None


@pytest.mark.asyncio
async def test_save_persists_the_provider(db_session: AsyncSession) -> None:
    """推論プロバイダが保存されること。

    モデル名だけでは実装が特定できない。`OLLAMA_MODEL` は環境変数で上書きでき、設定の
    sha256 にも現れない。同じモデルを別プロバイダ経由で使ったときに記録上
    Ollama と区別がつかないと、**測定値の比較可能性を後から検証できない**。

    `provider` は `LLMCallRecord` から DB 行まで運ばれる必要があるので、既定と非既定の
    両方を保存して結線を固定する。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.RUNBOOK)
    recorder(_record(chunk_index=0))
    recorder(_record(chunk_index=1, provider="stub"))

    saved = await save_design_llm_calls(
        db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
    )
    assert saved == 2

    rows = (
        (await db_session.execute(select(DesignLlmCall).order_by(DesignLlmCall.chunk_index)))
        .scalars()
        .all()
    )
    assert [r.provider for r in rows] == ["ollama", "stub"]


@pytest.mark.asyncio
async def test_save_records_failed_calls(db_session: AsyncSession) -> None:
    """失敗した呼び出しも記録すること。

    パースできなかった応答そのものが、モデルが何を返したかの唯一の手掛かりになる。
    ログには載せない方針なので、DB が唯一の記録になる。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.PLAN)
    recorder(_record(raw_output="{壊れたJSON", error="Expecting property name"))

    await save_design_llm_calls(
        db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
    )

    row = (await db_session.execute(select(DesignLlmCall))).scalars().one()
    assert row.error == "Expecting property name"
    assert row.raw_output == "{壊れたJSON"


@pytest.mark.asyncio
async def test_save_clips_oversized_fields_and_marks_truncated(db_session: AsyncSession) -> None:
    """保存上限で切り詰め、切り詰めた事実を残すこと（「全文がある」と誤認させない）。"""
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.SYSTEM)
    recorder(_record(user_message="あ" * (MAX_FIELD_CHARS + 100)))

    await save_design_llm_calls(
        db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
    )

    row = (await db_session.execute(select(DesignLlmCall))).scalars().one()
    assert row.truncated is True
    assert row.user_message.endswith("（保存上限で切り詰め）")
    assert len(row.user_message) < MAX_FIELD_CHARS + 100


@pytest.mark.asyncio
async def test_save_is_noop_without_records(db_session: AsyncSession) -> None:
    """記録が無ければ何も保存しないこと（AI を使わない経路で空行を作らない）。"""
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    assert (
        await save_design_llm_calls(
            db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
        )
        == 0
    )
    assert (await db_session.execute(select(DesignLlmCall))).scalars().all() == []


# --------------------------------------------------------------------------- #
# 保存失敗時の扱い（成果物を落とさない）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_safely_swallows_guard_error_and_saves_nothing(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """ガードが発火しても例外を投げず、行は作らず、検知を error として残すこと。

    ルートはこのラッパを使うので、来歴の保存失敗で AI 成果物（プラン・手順書・草案）を
    落とさない。ただし「記録に失敗した」ことが残らないと、確認書の開示欄で
    「AI を使っていない」と区別できなくなる。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.SYSTEM)
    recorder.declare_content_sources([_content_unit(Kind.FILE, file_role="patient_data")])
    recorder(_record())

    with caplog.at_level("WARNING"):
        saved = await save_design_llm_calls_safely(
            db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
        )

    assert saved == 0
    assert (await db_session.execute(select(DesignLlmCall))).scalars().all() == []
    assert any(r.levelname == "ERROR" and "ガードにより" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_safely_swallows_db_error_and_logs_warning(
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB エラーでも例外を投げず、warning を残すこと（成果物を落とさない）。

    ガード発火（error）とは区別できるログレベルにしてある。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.PLAN)
    recorder(_record())

    async def _boom() -> None:
        raise RuntimeError("接続が切れた")

    monkeypatch.setattr(db_session, "commit", _boom)

    with caplog.at_level("WARNING"):
        saved = await save_design_llm_calls_safely(
            db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
        )

    assert saved == 0
    assert any(
        r.levelname == "WARNING" and "保存に失敗" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_save_persists_the_model_digest(db_session: AsyncSession) -> None:
    """モデルのダイジェストが**保存した全行**に残ること。

    タグだけでは実装が特定できない。`ollama pull` で上流が更新されても、ローカルで同名
    タグを作り直しても、`model` 列は同じ文字列のままである（「`.env` を
    書き換えるだけでモデルが変わる」問題の、タグだけでは塞げない残り）。ダイジェストがあれば**同じタグで中身が
    違うモデルを使った2回の実行を記録だけで区別できる**。

    索引の同一性と同じく、**呼び出し1回ごとではなく宣言1回**を全行へ写す
    （1要求の生成は同じモデルで行われるので、行ごとに引き直す往復に意味が無い）。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    await recorder.declare_model_digest(
        _listing_client("model-a:12b", "sha256:abc123"), "model-a:12b"
    )
    recorder(_record(chunk_index=0))
    recorder(_record(chunk_index=1))

    saved = await save_design_llm_calls(
        db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
    )
    assert saved == 2

    rows = (
        (await db_session.execute(select(DesignLlmCall).order_by(DesignLlmCall.chunk_index)))
        .scalars()
        .all()
    )
    assert [r.model_digest for r in rows] == ["sha256:abc123", "sha256:abc123"]
    assert [r.model_digest_unavailable for r in rows] == [None, None]


@pytest.mark.asyncio
async def test_save_persists_why_the_digest_is_missing(db_session: AsyncSession) -> None:
    """取れなかったときは**理由**が残ること。

    `model_digest` が null になる意味は2つある——「引いたが取れなかった」と「宣言して
    いない」。**両方を null で表すと区別できない**ので、前者は理由の列を埋める
    （索引の同一性の「空配列と null は意味が違う」と同じ規則）。

    ここが null のまま区別を失うと、後から記録を読む人は「この実行はダイジェストを
    記録する版だったのか、それとも取れなかったのか」を答えられない。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    # 一覧の経路を持たない client（OpenAI 互換アダプタ相当）。
    await recorder.declare_model_digest(SimpleNamespace(provider_name="stub"), "model-e")
    recorder(_record())

    await save_design_llm_calls(
        db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
    )
    row = (await db_session.execute(select(DesignLlmCall))).scalars().one()
    assert row.model_digest is None
    assert row.model_digest_unavailable is not None
    assert "stub" in row.model_digest_unavailable


@pytest.mark.asyncio
async def test_not_declaring_the_digest_leaves_both_columns_null(db_session: AsyncSession) -> None:
    """宣言しなければ**両方 null** であること。

    これが「記録していない」の表現である。既定値で埋めると「引いたが取れなかった」と
    区別できなくなる。
    """
    qid = await _questionnaire(db_session)
    recorder = DesignLlmRecorder(DesignRole.RUNBOOK)
    recorder(_record())

    await save_design_llm_calls(
        db_session, questionnaire_id=qid, recorder=recorder, created_by_id=None
    )
    row = (await db_session.execute(select(DesignLlmCall))).scalars().one()
    assert row.model_digest is None
    assert row.model_digest_unavailable is None


def test_every_design_role_declares_the_model_digest() -> None:
    """**5つの設計ロールすべて**が宣言していること（結線の再発検知）。

    列と `declare_model_digest` があっても、ロールが呼ばなければ記録は永久に null で、
    それは「記録していない」と読める。索引の同一性は臨床・所在の2ロールだけが
    呼ぶ（索引を読むロールだけ）が、**ダイジェストは LLM を呼ぶ全ロールが対象**である
    ——手順書・DSL の生成も「どのモデルの中身で作ったか」を答えられなければならない。

    **守らないもの**: 新しく足したロールがこの一覧に載ることは保証しない（一覧はここに
    列挙してある5つだけ）。守るのは既存5ロールから宣言が消えることの検出である。
    """
    import inspect

    from kawasemi.ai_input import (
        clinical_drafter,
        compiler,
        location_drafter,
        resource_drafter,
        runbook,
    )

    missing = [
        module.__name__
        for module in (clinical_drafter, location_drafter, resource_drafter, runbook, compiler)
        if "declare_model_digest" not in inspect.getsource(module)
    ]
    assert not missing, f"ダイジェストを宣言していない設計ロール: {missing}"


# --------------------------------------------------------------------------- #
# ダイジェストの結線を**ロールごとに振る舞いで**測る
#
# `inspect.getsource` に文字列があることを見るだけでは足りない。**呼び出しが到達不能に
# なっても緑のまま**である（実測: `isinstance(recorder, DesignLlmRecorder)` を
# `isinstance(recorder, str)` へ変える変異でフルスイート 2,379 件が全通しした）。
# LLM を呼ぶ経路を実際に通して、recorder に値が入ることを固定する。
# --------------------------------------------------------------------------- #

_DIGEST = "4eb23ef187e2c5461e824f7c0d1f0c9b2f2fbb5a7c58b7a86e9a1d0f3c4b5a69"


def _fake_llm_client(payload: dict[str, Any], *, digest: str = _DIGEST) -> SimpleNamespace:
    """`chat` と `list` の両方を持つフェイク client（Ollama 相当）。

    `digest` は**実測の形**（`/api/tags` は64桁 hex を返し、`sha256:` 接頭辞は付かない）。
    """

    async def chat(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))

    async def _list() -> SimpleNamespace:
        return SimpleNamespace(models=[SimpleNamespace(model="model-a:12b", digest=digest)])

    return SimpleNamespace(chat=chat, list=_list)


@pytest.mark.asyncio
async def test_the_runbook_role_declares_the_digest() -> None:
    """手順書生成が宣言すること。"""
    from kawasemi.ai_input.runbook import AIRunbookGenerator

    drafter = AIRunbookGenerator()
    drafter._client = _fake_llm_client({"runbooks": []})  # type: ignore[assignment]
    recorder = DesignLlmRecorder(DesignRole.RUNBOOK)
    await drafter.generate(
        [{"link_id": "item1", "item_text": "診断日", "annotation": "x", "location": "y"}],
        model="model-a:12b",
        recorder=recorder,
    )
    assert recorder.model_digest == _DIGEST
    assert recorder.model_digest_unavailable is None


@pytest.mark.asyncio
async def test_the_plan_role_declares_the_digest() -> None:
    """DSL コンパイル（取得プラン）が宣言すること。"""
    from kawasemi.ai_input.compiler import AIPlanCompiler

    compiler = AIPlanCompiler()
    compiler._client = _fake_llm_client({"plans": []})  # type: ignore[assignment]
    recorder = DesignLlmRecorder(DesignRole.PLAN)
    await compiler.compile(
        [{"link_id": "item1", "item_text": "診断日", "runbook": "手順", "location": "所在"}],
        model="model-a:12b",
        recorder=recorder,
    )
    assert recorder.model_digest == _DIGEST
    assert recorder.model_digest_unavailable is None


@pytest.mark.asyncio
async def test_a_hanging_listing_does_not_block_the_generation() -> None:
    """`list()` が**固まっても**生成が始まること。

    ダイジェストの取得は生成の全体デッドライン（`llm_total_timeout`）の**外**で走るので、
    ここに独自の上限が無いと `settings.ollama_timeout`（環境によっては900秒）だけ無音に
    なる。**項目の生成が始まらない**形なので、例外を投げる形とは別に固定する。
    """
    import kawasemi.ai_input.llm_provider as provider_mod
    from kawasemi.ai_input.runbook import AIRunbookGenerator

    client = _fake_llm_client({"runbooks": []})

    async def hanging_list() -> SimpleNamespace:
        await asyncio.sleep(3600)
        raise AssertionError("到達しない")

    client.list = hanging_list
    drafter = AIRunbookGenerator()
    drafter._client = client  # type: ignore[assignment]
    recorder = DesignLlmRecorder(DesignRole.RUNBOOK)

    # 上限そのものを短くして測る（本番の既定値で待つテストにはしない）
    original = provider_mod.DIGEST_TIMEOUT_SECONDS
    provider_mod.DIGEST_TIMEOUT_SECONDS = 0.05
    try:
        await asyncio.wait_for(
            drafter.generate(
                [{"link_id": "item1", "item_text": "診断日", "annotation": "x", "location": "y"}],
                model="model-a:12b",
                recorder=recorder,
            ),
            timeout=10,
        )
    finally:
        provider_mod.DIGEST_TIMEOUT_SECONDS = original

    assert recorder.model_digest is None
    assert recorder.model_digest_unavailable is not None
    assert "応答が無い" in recorder.model_digest_unavailable


@pytest.mark.asyncio
async def test_the_location_role_declares_the_digest() -> None:
    """データ所在（`AILocationDrafter`）が宣言すること。

    5ロールすべてに結線していても、振る舞いで測るテストが無いと、宣言を到達不能にする
    変異（`isinstance(recorder, DesignLlmRecorder)` を `isinstance(recorder, str)` に
    する）を当てても、このロールでは1件も落ちない。

    候補が1件も無いと LLM を呼ばないので（`with_hits` が空なら早期 return）、
    BM25 の検索結果を差し替えて候補ありの経路を通す。
    """
    from kawasemi.ai_input.db_definition import ColumnHit
    from kawasemi.ai_input.location_drafter import AILocationDrafter, _ItemContext

    drafter = AILocationDrafter()
    drafter._client = _fake_llm_client({"choices": []})  # type: ignore[assignment]
    hit = ColumnHit(
        cand_id=1,
        db_group="ehr",
        db_qualified="ehr.DOCUMENT",
        table_logical="文書明細",
        physical_name="NOTE_TYPE",
        logical_name="文書種別",
        type_raw="VARCHAR",
        pk=False,
        notes=None,
    )
    drafter._retrieve = lambda items: [  # type: ignore[method-assign]
        _ItemContext(link_id="item1", item_text="診断日", annotation="x", hits=[hit])
    ]
    recorder = DesignLlmRecorder(DesignRole.SYSTEM)

    await drafter.draft(
        [{"link_id": "item1", "item_text": "診断日", "annotation": "x"}],
        model="model-a:12b",
        recorder=recorder,
    )

    assert recorder.model_digest == _DIGEST
    assert recorder.model_digest_unavailable is None


@pytest.mark.asyncio
async def test_the_resource_role_declares_the_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """リソース（`AIResourceDrafter`）が宣言すること。

    こちらも振る舞いで測らないと、同じ変異で1件も落ちないロールである。

    索引が検索可能でなければ `AILocationDrafter` へフォールバックしてしまい、
    **測っているのが別のロールになる**ので、検索可能な索引に見せる。宣言は最初の
    LLM 呼び出し（`generate_hints`）の直前に行われるため、そこで打ち切って
    「LLM を呼ぶ前に宣言が済んでいること」を見る。埋め込み器の先まで進めるには
    実 Ollama が要り、このテストの主張とは関係が無い。
    """
    import kawasemi.ai_input.resource_drafter as mod
    from kawasemi.ai_input.resource_drafter import AIResourceDrafter

    class _StopAfterDeclaration(Exception):
        pass

    async def _stop(*args: Any, **kwargs: Any) -> Any:
        raise _StopAfterDeclaration

    monkeypatch.setattr(
        mod,
        "get_store",
        lambda _dir: SimpleNamespace(is_searchable=lambda _m: True, embedding_digest=None),
    )
    monkeypatch.setattr(mod, "generate_hints", _stop)

    drafter = AIResourceDrafter()
    drafter._client = _fake_llm_client({"choices": []})  # type: ignore[assignment]
    recorder = DesignLlmRecorder(DesignRole.SYSTEM)

    with pytest.raises(_StopAfterDeclaration):
        await drafter.draft(
            [{"link_id": "item1", "item_text": "診断日", "annotation": "x"}],
            model="model-a:12b",
            recorder=recorder,
        )

    assert recorder.model_digest == _DIGEST
    assert recorder.model_digest_unavailable is None
