"""AIResourceDrafter（embedding検索版データ所在AI案）のユニットテスト

- embedding経路: 索引を tmp に構築し、Ollama(chat) と embedder をモックして
  候補選択→location/source_category の決定的導出を検証。
- フォールバック経路: 索引未構築なら DB定義書 BM25(AILocationDrafter) へ委譲。
- derive: payload からの location 文字列・source_category 導出。
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from kawasemi_indexer.builder import build_db_index

from kawasemi.ai_input import resource_drafter as rd_mod
from kawasemi.ai_input.design_provenance import DesignLlmRecorder
from kawasemi.ai_input.llm_json import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OPTIONS,
    LARGE_CONTEXT_OPTIONS,
    strict_response_format,
)
from kawasemi.ai_input.location_drafter import LocationDraftResult
from kawasemi.ai_input.models import DesignRole, SourceCategory
from kawasemi.ai_input.resource_drafter import AIResourceDrafter
from kawasemi.ai_input.resource_index.column_fill import ColumnFillIndex
from kawasemi.ai_input.resource_index.derive import build_location, derive_source_category
from kawasemi.ai_input.resource_index.schema import Kind, ResourceHit, ResourceUnit
from kawasemi.ai_input.resource_index.store import get_store, reset_store_cache
from kawasemi.ai_input.schemas import LLMLocationChoices
from tests.ai_input.conftest import HashingEmbedder

FIXTURE_DIR = str(Path(__file__).resolve().parents[1] / "fixtures" / "db_definition")


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_store_cache()
    yield
    reset_store_cache()


@pytest.fixture
def index_dir(tmp_path) -> str:
    out = tmp_path / "resource_index"
    build_db_index(source=FIXTURE_DIR, out_dir=out, embedder=HashingEmbedder())
    return str(out)


def _is_hint_call(kwargs: dict) -> bool:
    """検索ヒントのコールか。所在提案は**2種類のコールを打つ**。

    `format` に載る JSON schema のトップレベルのキーで見分ける（`hints` か `choices` か）。
    モデル名やプロンプトで見分けると、プロンプトを直したときに黙って壊れる。
    """
    return "hints" in (kwargs.get("format") or {}).get("properties", {})


def _patch_chat(
    drafter: AIResourceDrafter,
    payload: dict,
    calls: list,
    hint_payload: dict | None = None,
    hint_calls: list | None = None,
) -> None:
    """`client.chat` を差し替える。`calls` には**選択のコールだけ**が入る。

    検索ヒントのコールは `hint_calls` に分けて集める。既定のヒント応答は
    空（`hints: []`）で、クエリ拡張も型加点も起きない＝この版より前と同じ検索になる。
    """

    async def fake_chat(**kwargs):
        if _is_hint_call(kwargs):
            if hint_calls is not None:
                hint_calls.append(kwargs)
            body = hint_payload if hint_payload is not None else {"hints": []}
        else:
            calls.append(kwargs)
            body = payload
        message = types.SimpleNamespace(content=json.dumps(body))
        return types.SimpleNamespace(message=message)

    drafter._client = types.SimpleNamespace(chat=fake_chat)


def _echo_chat(calls: list, *, chosen: int | None, fail_on_call: int | None = None):
    """提示された linkId をそのまま返す `client.chat` の代役。

    検索ヒントのコールは空の `hints` で答え、`calls` にも入れない
    （拡張も加点も起きない＝この版より前と同じ検索になる）。`fail_on_call` は
    **選択のコールの**何回目を空応答にするか。
    """

    async def fake_chat(**kwargs):
        if _is_hint_call(kwargs):
            return types.SimpleNamespace(
                message=types.SimpleNamespace(content=json.dumps({"hints": []}))
            )
        calls.append(kwargs)
        ids = [
            line.split(": ", 1)[1]
            for line in kwargs["messages"][1]["content"].splitlines()
            if line.startswith("### linkId: ")
        ]
        if fail_on_call is not None and len(calls) == fail_on_call:
            # 空応答（`chat_json` が AIServiceException にする経路のうち最も起きやすい）
            return types.SimpleNamespace(message=types.SimpleNamespace(content=""))
        payload = {"choices": [{"link_id": i, "chosen_cand_id": chosen} for i in ids]}
        return types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(payload)))

    return fake_chat


# --------------------------------------------------------------------------- #
# derive
# --------------------------------------------------------------------------- #


def test_derive_db_location_and_category():
    unit = ResourceUnit(
        unit_id="db:DEMO.LAB.BLAST_PCT",
        kind=Kind.DB,
        label="芽球割合",
        search_text="x",
        payload={
            "connection_id": "DEMO_DB",
            "db_qualified": "DEMO.LAB",
            "table_logical": "検査結果",
            "physical_name": "BLAST_PCT",
            "logical_name": "芽球割合",
        },
    )
    loc = build_location(unit)
    assert "DEMO_DB" in loc and "DEMO.LAB（検査結果）" in loc and "BLAST_PCT（芽球割合）" in loc
    assert derive_source_category(unit) == SourceCategory.LOCAL_DB


def test_item_code_units_on_the_same_column_get_distinct_locations():
    """項目コードで行を選ぶ単位（経過表の体温・脈拍）は、同じ列でも所在の文字列が分かれる。"""

    def vital(code: str, name: str) -> ResourceUnit:
        return ResourceUnit(
            unit_id=f"db:SRC.VITALS.NUM#{code}",
            kind=Kind.DB,
            label=name,
            search_text="x",
            payload={
                "connection_id": "dwh",
                "db_qualified": "SRC.VITALS",
                "table_logical": "バイタル記録",
                "physical_name": "NUM",
                "logical_name": "数値",
                "item_code": code,
                "item_name": name,
                "item_filter_column": "ITEM_CODE",
                "item_row_filters": [{"column": "DELETED", "equals": "0"}],
            },
        )

    temp = build_location(vital("BT", "体温"))
    pulse = build_location(vital("PR", "脈拍"))
    assert temp != pulse
    # 有効な行の条件も書く（手順書の生成は location しか受け取らない）
    assert temp.endswith("/ 項目: BT（体温） / 絞り込み: SRC.VITALS.ITEM_CODE='BT' AND DELETED='0'")


def test_derive_fhir_and_file():
    fhir = ResourceUnit(
        unit_id="fhir:obs:wbc",
        kind=Kind.FHIR,
        label="白血球数",
        search_text="x",
        payload={
            "resource_type": "Observation",
            "query_template": "?code=6690-2",
            "fhir_path": "valueQuantity.value",
        },
    )
    assert derive_source_category(fhir) == SourceCategory.FHIR
    assert "Observation" in build_location(fhir) and "valueQuantity.value" in build_location(fhir)

    f = ResourceUnit(
        unit_id="file:rep",
        kind=Kind.FILE,
        label="紹介状",
        search_text="x",
        payload={"path_pattern": "/reports/{patient_id}/*.pdf", "file_role": "patient_data"},
    )
    # patient_data ファイルは file カテゴリ（実行は file_extract）
    assert derive_source_category(f) == SourceCategory.FILE
    # reference ファイルは参照知識なので manual
    ref = ResourceUnit(
        unit_id="file:doc",
        kind=Kind.FILE,
        label="プロトコル",
        search_text="x",
        payload={"path": "protocol.md", "file_role": "reference"},
    )
    assert derive_source_category(ref) == SourceCategory.MANUAL
    assert "/reports/{patient_id}/*.pdf" in build_location(f)


def test_derive_master_location_and_category():
    # master（コード表）はDB常駐の参照語彙。値取得はDB経由なので接続 scope に従う。
    unit = ResourceUnit(
        unit_id="master:DEMO_DB:DEMO.MST.DISEASE:C92.0",
        kind=Kind.MASTER,
        label="急性骨髄性白血病",
        search_text="急性骨髄性白血病",
        payload={
            "connection_id": "DEMO_DB",
            "db_qualified": "DEMO.MST.DISEASE",
            "code": "C92.0",
            "label": "急性骨髄性白血病",
        },
    )
    # 未登録接続は local 扱い（db と同じ scope ロジック）
    assert derive_source_category(unit) == SourceCategory.LOCAL_DB
    loc = build_location(unit)
    assert "DEMO.MST.DISEASE" in loc and "C92.0（急性骨髄性白血病）" in loc


# --------------------------------------------------------------------------- #
# embedding 経路
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resource_draft_requires_chosen_cand_id_in_the_response_format(
    index_dir, monkeypatch
):
    """LLM へ渡すスキーマが **`chosen_cand_id` を必須にしている**こと（結線）。

    既定値つきのフィールドは `model_json_schema()` の `required` に入らないので、structured
    output は**モデルにキーの省略を許す**。**所在候補は所在の確からしさの判定の入力**で、`chosen_cand_id` が
    省略されると `recommended` が付かず画面の初期選択が「未選択」になるので、人が選ばなければ
    段階分布が `ABSENT`／判定不能へ倒れる（典拠側と同じ形の欠陥がこちらにもあった）。

    **`source_category` は必須にしない。** LLM の返した値は捨てられ、種別は
    `derive_source_category`（索引の payload から決定的に導出）が決める。**読まれない値を
    必須にしても測定は動かない**。
    """
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    calls: list = []
    _patch_chat(
        drafter,
        {"choices": [{"link_id": "q1", "chosen_cand_id": 0, "source_category": "manual"}]},
        calls,
    )

    await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])

    assert calls, "LLM を呼んでいる前提"
    required = calls[0]["format"]["$defs"]["LLMLocationChoice"]["required"]
    assert "chosen_cand_id" in required, required
    # **捨てられる値は要求しない**（この行が「要求していないこと」を固定する）
    assert "source_category" not in required, required


async def test_resource_draft_keeps_the_first_of_duplicated_entries(index_dir, monkeypatch):
    """**同じ項目に2エントリ返る応答で、後勝ちにしない**こと（所在の確からしさの判定の入力）。"""
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    _patch_chat(
        drafter,
        {
            "choices": [
                {"link_id": "q1", "chosen_cand_id": 0, "source_category": "local_db"},
                {"link_id": "q1", "chosen_cand_id": None, "source_category": "manual"},
            ]
        },
        [],
    )

    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])
    drafts = result.items

    assert drafts[0].candidates[0].recommended is True, "後勝ちで一推しが消えている"


def _chunk_item_counts(calls: list) -> list[int]:
    """各コールの user メッセージに何項目載ったかを数える。"""
    return [c["messages"][1]["content"].count("### linkId:") for c in calls]


@pytest.mark.asyncio
async def test_draft_splits_items_by_the_default_chunk_size(index_dir, monkeypatch):
    """**既定幅（`DEFAULT_CHUNK_SIZE` = 4）で分割している**こと。

    `resource_drafter` は `chunk_size` を渡さないので既定が効く。**所在候補は所在の確からしさの判定の入力**で、
    分割が潰れると `num_ctx` 超過で空 content になり、候補が1件も付かない。

    6項目なら `[4, 2]`——**コール数だけ見ると幅3（`[3, 3]`）と区別できない**ので項目数で見る
    （「9項目では刻み幅3と4を区別できない」という同型の穴が実際に出た）。
    """
    assert DEFAULT_CHUNK_SIZE == 4, "前提: 既定幅は4（変えたらこの期待値も変える）"
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    calls: list = []

    drafter._client = types.SimpleNamespace(chat=_echo_chat(calls, chosen=None))

    await drafter.draft(
        [{"link_id": f"q{i}", "item_text": "芽球割合", "annotation": None} for i in range(6)]
    )

    assert _chunk_item_counts(calls) == [4, 2], "既定幅（4）で分割されていない"


@pytest.mark.asyncio
async def test_a_failed_chunk_leaves_the_rest_and_reports_them_as_unattempted(
    index_dir, monkeypatch
):
    """**所在側も部分結果を扱う**。**この経路は所在の確からしさの判定の入力**である。

    3チャンク作り（12項目・既定幅4）、**2番目だけ空応答**にする。以前は
    `chat_json_chunked` が `AIServiceException` を上げてルートが 500 を返し、**1・3番目の
    チャンクの一推しも捨てられていた**（典拠側と同じ算術が当てはまる経路——同じ項目数・
    1コール数十秒）。

    要点は**「候補を見せたが一推しが返らなかった項目」を「妥当な候補なし」と混ぜないこと**である。
    混ぜると、押し直せば埋まる項目が「AI では所在が特定できなかった」に入り、所在の確からしさの
    段階（rubric）の分布が動く。
    """
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    calls: list = []

    drafter._client = types.SimpleNamespace(chat=_echo_chat(calls, chosen=0, fail_on_call=2))

    items = [{"link_id": f"q{i}", "item_text": "芽球割合", "annotation": None} for i in range(12)]
    result = await drafter.draft(items)

    assert _chunk_item_counts(calls) == [4, 4, 4], "失敗の次のチャンクが投げられていない"
    # **失敗したチャンクの項目だけが未試行**（順序ではなく応答の有無で決まる）
    assert set(result.unattempted_link_ids) == {"q4", "q5", "q6", "q7"}
    # 1・3番目のチャンクの一推しは残る（捨てていない）
    recommended = {d.link_id for d in result.items if any(c.recommended for c in d.candidates)}
    assert recommended == {"q0", "q1", "q2", "q3", "q8", "q9", "q10", "q11"}
    # **候補なしと混ざっていない**（探索はしているので候補は全項目に付く）
    assert result.no_hit_count == 0
    assert all(d.candidates for d in result.items)
    assert result.llm_model is not None


@pytest.mark.asyncio
async def test_nothing_is_unattempted_when_every_chunk_answers(index_dir, monkeypatch):
    """**正常系で「未試行」が発火しない**こと（`guard-must-not-fire-on-normal-path`）。

    未試行の別掲は「正常系検出器」になりやすい。全チャンクが応答したときは空であることを
    正常系そのもので固定する。
    """
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    calls: list = []

    drafter._client = types.SimpleNamespace(chat=_echo_chat(calls, chosen=0))

    result = await drafter.draft(
        [{"link_id": f"q{i}", "item_text": "芽球割合", "annotation": None} for i in range(12)]
    )
    assert len(calls) == 3, "前提: チャンクが3つできている"
    assert result.unattempted_link_ids == ()
    assert result.duplicated_link_ids == ()


@pytest.mark.asyncio
async def test_the_index_path_reports_the_duplicated_items_to_the_caller(index_dir, monkeypatch):
    """**重複エントリの一覧を呼び出し側へ返す**こと（索引経路）。

    値を空にする変異が素通りしていた（落ちるのは重複のログのテストだけだった）。
    """
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    calls: list = []
    _patch_chat(
        drafter,
        {
            "choices": [
                {"link_id": "q1", "chosen_cand_id": 0},
                {"link_id": "q1", "chosen_cand_id": None},
            ]
        },
        calls,
    )

    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])

    assert result.duplicated_link_ids == ("q1",), "重複の一覧が呼び出し側へ渡っていない"
    assert result.items[0].candidates[0].recommended is True
    assert result.unattempted_link_ids == ()


@pytest.mark.asyncio
async def test_a_null_choice_is_not_counted_as_unattempted(index_dir, monkeypatch):
    """**`chosen_cand_id=null`（妥当な候補なし）は未試行ではない**。

    「エントリが返ったが null」は**結果**であって、押し直しても同じ答えになる。混ぜると
    「再実行すれば埋まる項目」の件数が過大になり、所在の確からしさの集計もずれる。
    """
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    calls: list = []
    _patch_chat(drafter, {"choices": [{"link_id": "q1", "chosen_cand_id": None}]}, calls)

    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])
    assert result.unattempted_link_ids == (), "妥当な候補なしが未試行に数えられている"
    assert result.items[0].candidates, "前提: 候補は提示されている"


async def test_draft_embedding_selects_and_derives(index_dir, monkeypatch):
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = (
        HashingEmbedder().model
    )  # フィクスチャ索引のモデルに一致させ embedding 経路を強制
    calls: list = []
    # LLM は cand_id を返すだけ（source_category は db のため無視され local_db に導出）
    _patch_chat(
        drafter,
        {"choices": [{"link_id": "q1", "chosen_cand_id": 0, "source_category": "manual"}]},
        calls,
    )

    recorder = DesignLlmRecorder(DesignRole.SYSTEM)
    result = await drafter.draft(
        [{"link_id": "q1", "item_text": "芽球割合", "annotation": None}], recorder=recorder
    )
    drafts = result.items

    # プロンプトへ中身（snippet）を入れたユニットの出所を宣言していること。
    # 宣言が漏れると、reference 以外のファイルの中身が入っても検査を
    # すり抜ける。このフィクスチャの索引は DB 定義書だけなので出所は {"db"} になる。
    assert recorder.content_sources == {"db"}
    # **所在提案は2回 LLM を呼ぶ**。検索ヒント（クエリ拡張と型）を先に取り、
    # そのあとで候補から選ばせる。**どちらも来歴に残る**——処理時間は
    # この2本の合計で数える必要があり、片方が記録されないと過小に出る。
    assert [r.label for r in recorder.records] == ["所在検索ヒント", "データ所在AI案"]

    assert result.no_hit_count == 0
    assert len(drafts) == 1
    cands = drafts[0].candidates
    assert cands, "top-k候補が返るべき"
    rec = cands[0]
    # LLM一推し（chosen_cand_id=0）は先頭に繰り上がり recommended=True
    assert rec.recommended is True
    assert rec.source_category == SourceCategory.LOCAL_DB  # 種別から決定的導出
    assert "BLAST_PCT（芽球割合）" in rec.location
    # 索引由来の機械可読情報を候補として保持（ステップ3のop引数素材）
    assert rec.unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert rec.connection_id == "DEMO_DB"
    assert rec.payload["physical_name"] == "BLAST_PCT"
    assert 1 <= rec.stars <= 3  # 適合度★（cosine→段階）
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_draft_embedding_null_choice_no_recommendation(index_dir, monkeypatch):
    """LLMが一推しを返さない（null）場合: 候補は提示するがどれも recommended=False。"""
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model  # embedding 経路を強制（フォールバック回避）
    _patch_chat(
        drafter,
        {"choices": [{"link_id": "q1", "chosen_cand_id": None, "source_category": "fhir"}]},
        [],
    )
    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])
    drafts = result.items
    cands = drafts[0].candidates
    assert cands, "一推しが無くても検索候補は提示する"
    assert all(not c.recommended for c in cands)


@pytest.mark.asyncio
async def test_draft_empty_items(index_dir):
    drafter = AIResourceDrafter(index_dir=index_dir)
    result = await drafter.draft([])
    drafts = result.items
    assert drafts == [] and result.no_hit_count == 0


# --------------------------------------------------------------------------- #
# フォールバック経路（索引未構築 → DB定義書 BM25）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_draft_falls_back_to_bm25_when_index_missing(tmp_path, monkeypatch):
    captured: dict = {}

    class _FakeBM25:
        def __init__(self, corpus_dir=None):
            captured["corpus_dir"] = corpus_dir

        async def draft(self, items, model=None, recorder=None):
            # フォールバック経路でも来歴を残すため、
            # 実物と同じ引数を受け取れる形にしておく（受け取れないとここで TypeError）。
            captured["called"] = True
            captured["recorder"] = recorder
            # フォールバック先も `LocationDraftResult` を返す。
            return LocationDraftResult(items=[], no_hit_count=1, llm_model="bm25-model")

    monkeypatch.setattr(rd_mod, "AILocationDrafter", _FakeBM25)
    drafter = AIResourceDrafter(index_dir=str(tmp_path / "absent"), corpus_dir="some/corpus")

    recorder = DesignLlmRecorder(DesignRole.SYSTEM)
    result = await drafter.draft(
        [{"link_id": "q1", "item_text": "芽球割合", "annotation": None}], recorder=recorder
    )
    drafts = result.items
    assert captured.get("called") is True
    assert captured.get("corpus_dir") == "some/corpus"
    # **渡した recorder がフォールバック先へそのまま届くこと**。
    # 受け取れることだけを確認しても `recorder=None` へ改変すると素通りするので、
    # 同一オブジェクトであることを固定する（決定性ではなく検出力を足す）。
    assert captured.get("recorder") is recorder
    assert drafts == [] and result.no_hit_count == 1
    # **フォールバック先の結果をそのまま返す**（件数と未試行を作り直さない）
    assert result.llm_model == "bm25-model"


class _DigestEmbedder(HashingEmbedder):
    """クエリ側のダイジェストを名乗る embedder。"""

    def __init__(self, digest=None):
        super().__init__()
        self._digest = digest

    def digest(self):
        return self._digest


async def test_draft_falls_back_to_bm25_when_the_embedding_weights_differ(index_dir, monkeypatch):
    """索引とクエリで embedding の重みが違うなら、索引が検索不可のときと同じく BM25 へ。

    ヒント生成（LLM 呼び出し）の**前**に判定する——使えない検索のための呼び出しを無駄にしない。
    """
    captured: dict = {}

    class _FakeBM25:
        def __init__(self, corpus_dir=None):
            pass

        async def draft(self, items, model=None, recorder=None):
            captured["fallback"] = True
            return LocationDraftResult(items=[], no_hit_count=1, llm_model="bm25-model")

    async def _no_hints(*a, **k):
        captured["hints_called"] = True
        raise AssertionError("使えない検索のためにヒントを生成した")

    monkeypatch.setattr(rd_mod, "AILocationDrafter", _FakeBM25)
    monkeypatch.setattr(rd_mod, "generate_hints", _no_hints)
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: _DigestEmbedder("bb" * 32))
    store = get_store(index_dir)
    store.build_info.embedding_digest = "aa" * 32
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model

    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])

    assert captured.get("fallback") is True
    assert "hints_called" not in captured
    assert result.llm_model == "bm25-model"


async def test_resource_draft_logs_duplicated_entries(index_dir, monkeypatch, caplog):
    """重複エントリを**ログに残す**こと（所在側の唯一の記録）。"""
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    _patch_chat(
        drafter,
        {
            "choices": [
                {"link_id": "q1", "chosen_cand_id": 0, "source_category": "local_db"},
                {"link_id": "q1", "chosen_cand_id": None, "source_category": "manual"},
            ]
        },
        [],
    )

    with caplog.at_level("WARNING"):
        await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])

    assert any("複数のエントリ" in r.message for r in caplog.records), (
        "重複がログに残っていない（所在側の唯一の記録）"
    )


@pytest.mark.asyncio
async def test_resource_draft_does_not_log_for_normal_responses(index_dir, monkeypatch, caplog):
    """重複が無いときは警告を出さないこと（**正常系で発火しない**）。

    `location_drafter` には対応するテストがあったが、こちらは発火側だけを測っていた。
    `if merged.duplicated_link_ids:` を `if True:` へ変えるミューテーションが**素通り**すると
    実測された——ガードが「正常系検出器」になっても気づけない状態である。
    """
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    _patch_chat(
        drafter,
        {"choices": [{"link_id": "q1", "chosen_cand_id": 0, "source_category": "local_db"}]},
        [],
    )

    with caplog.at_level("WARNING"):
        result = await drafter.draft(
            [{"link_id": "q1", "item_text": "芽球割合", "annotation": None}]
        )
        drafts = result.items

    assert drafts and drafts[0].candidates, "前提: 正常系として候補が返っている"
    assert not any("複数のエントリ" in r.message for r in caplog.records)


# --- プロンプト予算と候補本文の上限 ----------------------------------------------

# 出力ぶんの予約（1項目あたり）。返すのは `chosen_cand_id` と短い理由なので、臨床的意味の
# 下書き（`clinical_drafter._OUTPUT_RESERVE_CHARS = 500`）と同じ会計にする。
_OUTPUT_RESERVE_CHARS = 500


def _budget_contexts(chunk_size: int, snippet_chars: int) -> list[rd_mod._ItemContext]:
    """1コールに詰める最大構成（`_TOP_K` 件 × `chunk_size` 項目）。"""
    contexts = []
    for i in range(chunk_size):
        hits = [
            ResourceHit(
                cand_id=i * rd_mod._TOP_K + j,
                unit=ResourceUnit(
                    unit_id=f"file:登録入力マニュアル_2026.pdf#p{j}:0-800",
                    kind=Kind.FILE,
                    label="登録入力マニュアル_2026.pdf p.12",
                    snippet="あ" * snippet_chars,
                    search_text="",
                    payload={"file_role": "reference"},
                ),
                score=0.9,
            )
            for j in range(rd_mod._TOP_K)
        ]
        contexts.append(
            rd_mod._ItemContext(
                link_id=f"item-{i:04d}",
                item_text="項目名" * 10,
                annotation="この項目の臨床的な意味の説明。" * 20,
                hits=hits,
            )
        )
    return contexts


def test_long_snippets_are_clipped() -> None:
    """候補本文が `_MAX_SNIPPET_CHARS` で切り詰められること。

    **索引側が切っていることに依存しない。** いまの `kawasemi_indexer` は
    `snippet=piece[:200]` で切っているが、それは索引側の実装の都合であって
    こちらの保証ではない。索引の作り方が変われば、`_TOP_K`(8) × 刻み幅(4) = 1コール32候補の
    本文がそのまま入る（snippet 800 文字なら3万字を超える）。
    """
    long_snippet = "あ" * (rd_mod._MAX_SNIPPET_CHARS * 4)
    contexts = _budget_contexts(1, snippet_chars=len(long_snippet))

    msg = AIResourceDrafter.__new__(AIResourceDrafter)._build_user_message(contexts)

    assert long_snippet not in msg
    assert msg.count("あ" * rd_mod._MAX_SNIPPET_CHARS) == rd_mod._TOP_K
    assert "あ" * (rd_mod._MAX_SNIPPET_CHARS + 1) not in msg


def test_resource_prompt_fits_the_num_ctx_budget() -> None:
    """**実際に組み立てたプロンプト**が num_ctx の予算に収まること。

    臨床的意味（`clinical_drafter`）と手順書（`runbook`）にはこの検査があったが、
    **location / resource には無かった**。測ってみると、索引の現状（snippet 200）
    でも **1万2千字前後**で既定の `num_ctx=8192` を超えていた——「刻み幅が潰れていない幅4が
    本当に収まるか」は誰も測っておらず、答えは**収まっていない**だった。

    そのためこの経路は `LARGE_CONTEXT_OPTIONS`（32768）で呼ぶ。単位が文字であってトークン
    ではないこと（1文字＝1トークンは上界ではない）は `clinical_drafter` の予算テストと同じ。
    ここで守るのは、`_TOP_K`・刻み幅・`_MAX_SNIPPET_CHARS` を上げたときに気づくことである。
    """
    contexts = _budget_contexts(DEFAULT_CHUNK_SIZE, snippet_chars=rd_mod._MAX_SNIPPET_CHARS)
    user_msg = AIResourceDrafter.__new__(AIResourceDrafter)._build_user_message(contexts)
    schema = json.dumps(
        strict_response_format(LLMLocationChoices, *rd_mod._REQUIRED_CHOICE_KEYS),
        ensure_ascii=False,
    )
    total = (
        len(rd_mod.DRAFT_SYSTEM_PROMPT)
        + len(schema)
        + len(user_msg)
        + _OUTPUT_RESERVE_CHARS * DEFAULT_CHUNK_SIZE
    )
    num_ctx = int(LARGE_CONTEXT_OPTIONS["num_ctx"])
    assert total <= num_ctx, {
        "system": len(rd_mod.DRAFT_SYSTEM_PROMPT),
        "schema": len(schema),
        "user": len(user_msg),
        "total": total,
        "num_ctx": num_ctx,
    }
    # 既定（8192）では収まらないことも書いておく（`options` の配線が外れたら気づく）。
    assert total > int(DEFAULT_OPTIONS["num_ctx"])


# --------------------------------------------------------------------------- #
# 規約文書を所在の候補にしない
# --------------------------------------------------------------------------- #


def _ref_unit() -> ResourceUnit:
    return ResourceUnit(
        unit_id="file:registry_manual.pdf#p15:0-631",
        kind=Kind.FILE,
        label="registry_manual.pdf（p15）",
        search_text="[15] 施設名 施設の名称。",
        snippet="[15] 施設名",
        payload={"file_role": "reference", "path": "registry_manual.pdf", "page": 15},
    )


def _db_unit() -> ResourceUnit:
    return ResourceUnit(
        unit_id="db:SRC.PATIENT_INFO.FACILITY_NAME",
        kind=Kind.DB,
        label="医療機関名",
        search_text="患者基本 FACILITY_NAME 医療機関名",
        snippet="DB=dwh 表=SRC.PATIENT_INFO",
        payload={
            "connection_id": "dwh",
            "db_qualified": "SRC.PATIENT_INFO",
            "column": "FACILITY_NAME",
        },
    )


@pytest.mark.asyncio
async def test_reference_units_are_not_offered_as_location_candidates(monkeypatch):
    """所在の候補から規約文書を外す。

    `file` には `file_role=patient_data`（値の実物）と `reference`（入力マニュアル等）の
    両方が入る。**規約文書は意味の典拠であって、データの所在ではない。** 除外が無かった
    ため、項目名で検索するとマニュアルの定義文が上位を占め、略号の列名しか持たない DB 側が
    負けていた（実際の索引では候補の大半がマニュアル）。
    """
    passed: dict = {}

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
            # 第1段が空＝絞り込まない。この検査は第2段の predicate を見る。
            return []

        def search(self, qv, top_k, min_score, predicate=None):  # noqa: ANN001, ARG002
            passed["predicate"] = predicate
            units = [_ref_unit(), _db_unit()]
            kept = [u for u in units if predicate is None or predicate(u)]
            return [ResourceHit(unit=u, score=1.0, cand_id=i) for i, u in enumerate(kept)]

    monkeypatch.setattr(rd_mod, "get_store", lambda _dir: _Store())
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())

    drafter = AIResourceDrafter()
    _patch_chat(
        drafter,
        {"choices": [{"link_id": "facility_name", "chosen_cand_id": 0}]},
        [],
    )
    result = await drafter.draft(
        [{"link_id": "facility_name", "item_text": "[15] 施設名", "annotation": None}]
    )

    assert passed["predicate"] is not None, "所在の検索に述語が渡されていない"
    assert passed["predicate"](_db_unit()) is True
    assert passed["predicate"](_ref_unit()) is False, "規約文書が候補に残っている"

    offered = [c.unit_id for item in result.items for c in item.candidates]
    assert offered == ["db:SRC.PATIENT_INFO.FACILITY_NAME"], offered


@pytest.mark.asyncio
async def test_the_generated_terms_reach_the_embedding_query(index_dir, monkeypatch):
    """検索ヒントの語が**実際に検索クエリへ渡っている**こと（結線）。

    定数として固定できるのは「何を要求するか」（プロンプトと上限）だけで、生成した語がクエリに
    入らないまま提案が動いても定数は1バイトも変わらない。埋め込みに渡った文字列を
    捕まえて測る。
    """
    captured: list[list[str]] = []

    class _CapturingEmbedder(HashingEmbedder):
        def embed(self, texts):
            captured.append(list(texts))
            return super().embed(texts)

    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: _CapturingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    calls: list = []
    hint_calls: list = []
    _patch_chat(
        drafter,
        {"choices": [{"link_id": "q1", "chosen_cand_id": 0}]},
        calls,
        hint_payload={
            "hints": [{"link_id": "q1", "terms": ["血液検査", "BLAST"], "expected_types": []}]
        },
        hint_calls=hint_calls,
    )

    await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])

    assert captured, "embedding が呼ばれていない"
    assert captured[0] == ["芽球割合 血液検査 BLAST"], "生成した語がクエリへ入っていない"
    # **ヒントも選択も同じモデルで呼ぶ**。モデルどうしを比べるとき、
    # 別のモデルに生成させると比較が交絡する。
    assert hint_calls[0]["model"] == calls[0]["model"]


@pytest.mark.asyncio
async def test_a_failed_hint_call_does_not_stop_the_draft(index_dir, monkeypatch):
    """ヒントの生成が落ちても所在提案は出ること。

    ヒントは検索を良くするための施策であって提案の前提条件ではない。ここで落ちると、
    施策を入れたせいで所在候補そのものが取れなくなる。
    """
    monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model

    async def fake_chat(**kwargs):
        if _is_hint_call(kwargs):
            return types.SimpleNamespace(message=types.SimpleNamespace(content=""))
        payload = {"choices": [{"link_id": "q1", "chosen_cand_id": 0}]}
        return types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(payload)))

    drafter._client = types.SimpleNamespace(chat=fake_chat)

    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])
    assert result.items[0].candidates, "ヒントの失敗で候補が消えてはいけない"
    assert result.items[0].candidates[0].recommended is True


async def test_resource_draft_sends_embedding_to_embedding_base_url(index_dir, monkeypatch):
    """**embedding だけが `EMBEDDING_BASE_URL` へ行き、LLM は `OLLAMA_BASE_URL` のまま**。"""
    from kawasemi.core.config import settings

    monkeypatch.setattr(settings, "ollama_base_url", "http://llm-host:11500")
    monkeypatch.setattr(settings, "embedding_base_url", "http://embed-host:11434")
    embedder_kwargs: list[dict] = []

    def _embedder(**kw):
        embedder_kwargs.append(kw)
        return HashingEmbedder()

    monkeypatch.setattr(rd_mod, "OllamaEmbedder", _embedder)
    drafter = AIResourceDrafter(index_dir=index_dir)
    drafter._embedding_model = HashingEmbedder().model
    # LLM の client は設定を読んで作られる（差し替える前に接続先を見る）
    assert str(drafter._client._client.base_url) == "http://llm-host:11500"
    _patch_chat(drafter, {"choices": [{"link_id": "q1", "chosen_cand_id": 0}]}, [])

    await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])

    assert [kw["base_url"] for kw in embedder_kwargs] == ["http://embed-host:11434"]


def test_query_carries_the_parent_text_of_an_ambiguous_item():
    """「数値(mg/dL)」だけでは何の値か分からない。親（「CRP」）を前に付けて引く。"""
    from kawasemi.ai_input.location_hint import EMPTY_HINT, item_label

    item = {"link_id": "x", "item_text": "数値(mg/dL)", "parent_text": "CRP"}
    assert item_label(item) == "CRP / 数値(mg/dL)"
    assert rd_mod._query_text(item, EMPTY_HINT).startswith("CRP / 数値(mg/dL)")
    assert rd_mod._short_query_text(item, EMPTY_HINT) == "CRP / 数値(mg/dL)"
    # 親の text が項目の text に含まれるときは付けない。無題の欄は親の text で引く
    assert item_label({"item_text": "既往歴", "parent_text": "既往歴"}) == "既往歴"
    assert item_label({"item_text": None, "parent_text": "入院日"}) == "入院日"
    assert item_label({"item_text": "咳嗽"}) == "咳嗽"


def test_the_hint_and_choice_prompts_show_the_parent_text():
    """ヒントと候補選びの両方のプロンプトに「CRP / 数値(mg/dL)」が載る。"""
    from kawasemi.ai_input.location_hint import _build_hint_message

    item = {"link_id": "x", "item_text": "数値(mg/dL)", "parent_text": "CRP"}
    assert "項目名: CRP / 数値(mg/dL)" in _build_hint_message([item])

    ctx = rd_mod._ItemContext(
        link_id="x", item_text="数値(mg/dL)", annotation=None, hits=[], label="CRP / 数値(mg/dL)"
    )
    msg = rd_mod.AIResourceDrafter.__new__(rd_mod.AIResourceDrafter)._build_user_message([ctx])
    assert "項目名: CRP / 数値(mg/dL)" in msg
