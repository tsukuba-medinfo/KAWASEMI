"""ステップ1役割2「AIによる案の作成」のユニットテスト

- db_definition: トークナイザ / コーパスのロード / BM25検索 / location組み立て / コーパス未配置
- location_drafter: Ollamaをモックして 候補選択→location差し込み・カテゴリ補正・候補ゼロ集計

施設の定義書（site/db_definition, gitignore）には依存せず、架空の最小フィクスチャ
（tests/fixtures/db_definition）と Ollama モックで完結する。
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from kawasemi.ai_input import location_drafter as loc_mod
from kawasemi.ai_input.db_definition import (
    ColumnHit,
    DbDefinitionIndex,
    _tokenize,
    get_index,
    reset_index_cache,
)
from kawasemi.ai_input.llm_json import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OPTIONS,
    LARGE_CONTEXT_OPTIONS,
    strict_response_format,
)
from kawasemi.ai_input.location_drafter import AILocationDrafter
from kawasemi.ai_input.models import SourceCategory
from kawasemi.ai_input.schemas import LLMLocationChoices

FIXTURE_DIR = str(Path(__file__).resolve().parents[1] / "fixtures" / "db_definition")


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_index_cache()
    yield
    reset_index_cache()


# --------------------------------------------------------------------------- #
# トークナイザ
# --------------------------------------------------------------------------- #


def test_tokenize_ascii_and_bigram():
    tokens = _tokenize("BLAST_PCT 芽球割合")
    assert "blast_pct" in tokens  # ascii はNFKC/lowerで丸ごと1トークン
    assert "芽球" in tokens and "球割" in tokens and "割合" in tokens  # 日本語はbigram


def test_tokenize_empty():
    assert _tokenize("") == []
    assert _tokenize(None) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# コーパスのロードとBM25検索
# --------------------------------------------------------------------------- #


def test_index_loads_fixture():
    index = DbDefinitionIndex.load(FIXTURE_DIR)
    assert index.is_loaded
    assert index.column_count == 5


def test_search_finds_relevant_column():
    index = DbDefinitionIndex.load(FIXTURE_DIR)
    hits = index.search("芽球割合", top_k=3)
    assert hits, "ヒットが返るべき"
    assert hits[0].physical_name == "BLAST_PCT"
    assert hits[0].cand_id == 0  # 上位から連番
    assert hits[0].db_group == "DEMO_DB"


def test_search_no_match_returns_empty():
    index = DbDefinitionIndex.load(FIXTURE_DIR)
    assert index.search("まったく無関係なクエリxyz", top_k=5) == []
    assert index.search("", top_k=5) == []


def test_missing_corpus_is_empty():
    index = DbDefinitionIndex.load("/nonexistent/path/db_definition")
    assert not index.is_loaded
    assert index.search("芽球割合") == []


def test_build_location_format():
    index = DbDefinitionIndex.load(FIXTURE_DIR)
    hit = index.search("芽球割合", top_k=1)[0]
    loc = hit.build_location()
    assert "DEMO_DB" in loc
    assert "DEMO.LAB（検査結果）" in loc
    assert "BLAST_PCT（芽球割合）" in loc


def test_get_index_is_cached():
    first = get_index(FIXTURE_DIR)
    second = get_index(FIXTURE_DIR)
    assert first is second  # 同一ディレクトリはシングルトン


# --------------------------------------------------------------------------- #
# location_drafter（Ollamaモック）
# --------------------------------------------------------------------------- #


def _fake_chat_response(payload: dict):
    """ollama AsyncClient.chat の戻り（response.message.content）を模したオブジェクト"""
    message = types.SimpleNamespace(content=json.dumps(payload))
    return types.SimpleNamespace(message=message)


def _fake_chat_response_raw(content: str):
    """content をそのまま返す（空応答＝`chat_json` が失敗にする経路を作るため）"""
    return types.SimpleNamespace(message=types.SimpleNamespace(content=content))


def _patch_client(drafter: AILocationDrafter, payload: dict, calls: list):
    async def fake_chat(**kwargs):
        calls.append(kwargs)
        return _fake_chat_response(payload)

    drafter._client = types.SimpleNamespace(chat=fake_chat)


@pytest.mark.asyncio
async def test_draft_selects_candidate_and_builds_location():
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    calls: list = []
    # LLMは cand_id を返すだけ（local_db）
    _patch_client(
        drafter,
        {"choices": [{"link_id": "q1", "chosen_cand_id": 0, "source_category": "local_db"}]},
        calls,
    )

    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])
    drafts = result.items

    assert result.no_hit_count == 0
    assert len(drafts) == 1
    rec = drafts[0].candidates[0]
    assert rec.recommended is True  # LLM一推しは先頭に繰り上がる
    assert rec.source_category == SourceCategory.LOCAL_DB
    assert "BLAST_PCT（芽球割合）" in rec.location
    assert 1 <= rec.stars <= 3
    assert len(calls) == 1  # 候補ありなのでLLMが1回呼ばれる


@pytest.mark.asyncio
async def test_location_draft_requires_chosen_cand_id_in_the_response_format():
    """LLM へ渡すスキーマが **`chosen_cand_id` を必須にしている**こと（結線）。

    既定値つきのフィールドは `model_json_schema()` の `required` に入らないので、structured
    output は**モデルにキーの省略を許す**。**所在候補は所在の確からしさの段階（rubric）の入力**で、
    `chosen_cand_id` が省略されると `recommended` が付かず画面の初期選択が「未選択」になる
    ので、人が選ばなければ段階分布が `ABSENT`／判定不能へ倒れる。

    **`source_category` は必須にしない。** LLM の返した値は
    `choice_by_link = {c.link_id: c.chosen_cand_id ...}` で捨てられ、種別は
    `SourceCategory.LOCAL_DB` を直に置いている。**読まれない値を必須にしても測定は動かない**
    ので、要求しない（効かない要求を層として数えない）。

    `resource_drafter` と `clinical_drafter` にも同じ結線テストがある。**3経路すべてに置く**
    （1経路だけ測ると、他の経路が `model_json_schema()` へ戻っても誰も気づかない）。
    """
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    calls: list = []
    _patch_client(
        drafter,
        {"choices": [{"link_id": "q1", "chosen_cand_id": 0, "source_category": "local_db"}]},
        calls,
    )

    await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])

    assert calls, "LLM を呼んでいる前提"
    required = calls[0]["format"]["$defs"]["LLMLocationChoice"]["required"]
    assert "chosen_cand_id" in required, required
    # **捨てられる値は要求しない**（この行が「要求していないこと」を固定する）
    assert "source_category" not in required, required


@pytest.mark.asyncio
async def test_location_draft_keeps_the_first_of_duplicated_entries():
    """**同じ項目に2エントリ返る応答で、後勝ちにしない**こと（他の経路と同じ規則）。

    所在候補は**所在の確からしさの段階（rubric）の入力**である。後勝ちで `chosen_cand_id=null` を
    採ると `recommended` が付かず、画面の初期選択が「未選択」になり、人が選ばなければ段階分布が
    `ABSENT`／判定不能へ倒れる。
    """
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    _patch_client(
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


@pytest.mark.asyncio
async def test_draft_splits_items_by_the_default_chunk_size():
    """**既定幅（`DEFAULT_CHUNK_SIZE` = 4）で分割している**こと。

    6項目なら `[4, 2]`（コール数だけでは幅3 と区別できない）。この経路は索引が検索不可のときの
    縮退先なので、**分割が潰れると縮退したときだけ空 content になる**——気づきにくい。
    """
    assert DEFAULT_CHUNK_SIZE == 4, "前提: 既定幅は4（変えたらこの期待値も変える）"
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    calls: list = []

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        ids = [
            line.split(": ", 1)[1]
            for line in kwargs["messages"][1]["content"].splitlines()
            if line.startswith("### linkId: ")
        ]
        return _fake_chat_response(
            {"choices": [{"link_id": i, "chosen_cand_id": None} for i in ids]}
        )

    drafter._client = types.SimpleNamespace(chat=fake_chat)

    await drafter.draft(
        [{"link_id": f"q{i}", "item_text": "芽球割合", "annotation": None} for i in range(6)]
    )

    counts = [c["messages"][1]["content"].count("### linkId:") for c in calls]
    assert counts == [4, 2], "既定幅（4）で分割されていない"


@pytest.mark.asyncio
async def test_the_bm25_path_keeps_partial_results_and_reports_unattempted():
    """**縮退先（BM25 経路）でも部分結果を扱う**。

    索引が検索不可のときはこちらが動くので、**捨てると「索引を作っていない施設では
    一括生成が完走しない」**という形で残る。3チャンク作り、2番目だけ空応答にする。

    経路ごとに1本ずつ測るのは、**1経路だけ壊す退行が他経路のテストに助けられて
    生き残らないようにする**ためである。
    """
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    calls: list = []

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        ids = [
            line.split(": ", 1)[1]
            for line in kwargs["messages"][1]["content"].splitlines()
            if line.startswith("### linkId: ")
        ]
        if len(calls) == 2:
            return _fake_chat_response_raw("")  # 空応答
        return _fake_chat_response({"choices": [{"link_id": i, "chosen_cand_id": 0} for i in ids]})

    drafter._client = types.SimpleNamespace(chat=fake_chat)

    result = await drafter.draft(
        [{"link_id": f"q{i}", "item_text": "芽球割合", "annotation": None} for i in range(12)]
    )

    counts = [c["messages"][1]["content"].count("### linkId:") for c in calls]
    assert counts == [4, 4, 4], "失敗の次のチャンクが投げられていない"
    assert set(result.unattempted_link_ids) == {"q4", "q5", "q6", "q7"}
    recommended = {d.link_id for d in result.items if any(c.recommended for c in d.candidates)}
    assert recommended == {"q0", "q1", "q2", "q3", "q8", "q9", "q10", "q11"}
    assert result.no_hit_count == 0, "候補なしと混ざっている"


@pytest.mark.asyncio
async def test_the_bm25_path_reports_nothing_unattempted_on_the_normal_path():
    """**正常系で「未試行」が発火しない**こと（BM25 経路）。"""
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    calls: list = []

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        ids = [
            line.split(": ", 1)[1]
            for line in kwargs["messages"][1]["content"].splitlines()
            if line.startswith("### linkId: ")
        ]
        return _fake_chat_response({"choices": [{"link_id": i, "chosen_cand_id": 0} for i in ids]})

    drafter._client = types.SimpleNamespace(chat=fake_chat)
    result = await drafter.draft(
        [{"link_id": f"q{i}", "item_text": "芽球割合", "annotation": None} for i in range(12)]
    )
    assert len(calls) == 3, "前提: チャンクが3つできている"
    assert result.unattempted_link_ids == ()


@pytest.mark.asyncio
async def test_a_null_choice_is_not_counted_as_unattempted_on_the_bm25_path():
    """**`chosen_cand_id=null`（妥当な候補なし）は未試行ではない**（BM25 経路）。

    索引側にはこのテストがあったが**BM25 側に無かった**ので、判定を
    `choice_by_link.get(...) is None` へ差し替える変異が**この経路だけ生き残った**。
    索引が未構築の施設ではこちらが動くので、段階判定の入力の区別がそこで失われる
    （`test_each_defense_layer_alone` の型）。
    """
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    calls: list = []
    _patch_client(drafter, {"choices": [{"link_id": "q1", "chosen_cand_id": None}]}, calls)

    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])

    assert len(calls) == 1, "前提: 候補があるので LLM を呼んでいる"
    assert result.items[0].candidates, "前提: 候補は提示されている"
    assert result.unattempted_link_ids == (), "妥当な候補なしが未試行に数えられている"


@pytest.mark.asyncio
async def test_the_bm25_path_reports_the_duplicated_items_to_the_caller():
    """**重複エントリの一覧を呼び出し側へ返す**こと。

    ログにしか残らないと、集計は矛盾した応答だった項目を外せない。
    `LocationDraftResult` に載せた値を**非空で assert するテスト**が無いと、値を空にする
    変異が素通りする。
    """
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    calls: list = []
    _patch_client(
        drafter,
        {
            "choices": [
                {"link_id": "q1", "chosen_cand_id": 0},
                {"link_id": "q1", "chosen_cand_id": None},  # 同じ項目に2件目
            ]
        },
        calls,
    )

    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])

    assert result.duplicated_link_ids == ("q1",), "重複の一覧が呼び出し側へ渡っていない"
    # **最初のエントリを採る**（後勝ちにしない）
    assert result.items[0].candidates[0].recommended is True
    assert result.unattempted_link_ids == (), "重複は未試行ではない"


@pytest.mark.asyncio
async def test_the_model_name_is_absent_when_the_llm_was_never_called():
    """**LLM を1回も呼んでいなければモデル名を出さない**。

    ルートは以前 `model or settings.ollama_model` を自分で組み立てていたので、**候補が0件で
    LLM を呼んでいない実行でもモデル名を返していた**（使っていないモデルを「使った」と開示する）。
    """
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    calls: list = []
    _patch_client(drafter, {"choices": []}, calls)

    result = await drafter.draft(
        [{"link_id": "q1", "item_text": "まったく無関係xyz", "annotation": None}]
    )
    assert calls == [], "前提: 候補ゼロなので LLM は呼ばれない"
    assert result.no_hit_count == 1
    assert result.llm_model is None, "呼んでいないモデル名を出している"


@pytest.mark.asyncio
async def test_draft_bm25_candidates_are_local_db():
    """BM25候補（DB定義書のカラム）は常に local_db で提示される"""
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    _patch_client(
        drafter,
        {"choices": [{"link_id": "q1", "chosen_cand_id": 0, "source_category": "manual"}]},
        [],
    )
    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])
    drafts = result.items
    assert all(c.source_category == SourceCategory.LOCAL_DB for c in drafts[0].candidates)
    assert drafts[0].candidates[0].location is not None


@pytest.mark.asyncio
async def test_draft_null_choice_no_recommendation():
    """一推しが無くてもBM25候補は提示するが、どれも recommended=False。"""
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    _patch_client(
        drafter,
        {"choices": [{"link_id": "q1", "chosen_cand_id": None, "source_category": "fhir"}]},
        [],
    )
    result = await drafter.draft([{"link_id": "q1", "item_text": "芽球割合", "annotation": None}])
    drafts = result.items
    cands = drafts[0].candidates
    assert cands
    assert all(not c.recommended for c in cands)


@pytest.mark.asyncio
async def test_draft_no_candidates_skips_llm():
    """候補ゼロの項目はLLMを呼ばず、候補なし(candidates=[])として返り result.no_hit_count に計上される"""
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    calls: list = []
    _patch_client(drafter, {"choices": []}, calls)

    result = await drafter.draft(
        [{"link_id": "q1", "item_text": "まったく無関係xyz", "annotation": None}]
    )
    drafts = result.items
    assert len(drafts) == 1
    assert drafts[0].candidates == []
    assert result.no_hit_count == 1
    assert calls == []  # 候補ゼロのみならLLMは呼ばれない


@pytest.mark.asyncio
async def test_draft_empty_items():
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    result = await drafter.draft([])
    drafts = result.items
    assert drafts == [] and result.no_hit_count == 0


@pytest.mark.asyncio
async def test_location_draft_logs_duplicated_entries(caplog: pytest.LogCaptureFixture) -> None:
    """重複エントリを**ログに残す**こと。

    所在側は戻り値がタプルなので重複の一覧を呼び出し側へ返せない。
    **いまの記録はこのログだけ**なので、消えると矛盾した応答が痕跡なしになる。
    """
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    _patch_client(
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
async def test_location_draft_does_not_log_for_normal_responses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """重複が無いときは警告を出さないこと（**正常系で発火しない**）。"""
    drafter = AILocationDrafter(corpus_dir=FIXTURE_DIR)
    _patch_client(
        drafter,
        {"choices": [{"link_id": "q1", "chosen_cand_id": 0, "source_category": "local_db"}]},
        [],
    )

    with caplog.at_level("WARNING"):
        result = await drafter.draft(
            [{"link_id": "q1", "item_text": "芽球割合", "annotation": None}]
        )
        drafts = result.items

    # **前提を assert として置く**（resource 側と揃える）。
    # 候補が空なら LLM の応答を読む経路に入らないので、この検査は何も測らない
    assert drafts and drafts[0].candidates, "前提: 正常系として候補が返っている"
    assert not any("複数のエントリ" in r.message for r in caplog.records)


# --- プロンプト予算 -------------------------------------------------------------

# 出力ぶんの予約（1項目あたり）。返すのは `chosen_cand_id` と短い理由なので、臨床的意味の
# 下書き（`clinical_drafter._OUTPUT_RESERVE_CHARS = 500`）と同じ会計にする。
_OUTPUT_RESERVE_CHARS = 500


def test_location_prompt_fits_the_num_ctx_budget() -> None:
    """**実際に組み立てたプロンプト**が num_ctx の予算に収まること。

    臨床的意味と手順書にはこの検査があったが、location / resource には無かった。
    最大構成では **1万2千字前後**になり、既定の `num_ctx=8192` を超えていた（候補カラム
    8件 × 4項目に、表・カラムの論理名と型とメモが付く）。そのためこの経路は
    `LARGE_CONTEXT_OPTIONS`（32768）で呼ぶ。

    単位は文字であってトークンではない（1文字＝1トークンは上界ではない）。ここで守るのは、
    `_TOP_K` や刻み幅を上げたとき、あるいは候補1件あたりの提示項目を増やしたときに
    気づくことである。
    """
    contexts = [
        loc_mod._ItemContext(
            link_id=f"item-{i:04d}",
            item_text="項目名" * 10,
            annotation="この項目の臨床的な意味の説明。" * 20,
            hits=[
                ColumnHit(
                    cand_id=i * loc_mod._TOP_K + j,
                    db_group="DWH",
                    db_qualified=f"DEMO{j}.PATIENT_INFOMATION_LONG",
                    table_logical="患者基本情報テーブル（がん登録用）",
                    physical_name=f"BLAST_PCT_COLUMN_{j}",
                    logical_name="末梢血白血球中の芽球割合（％）",
                    type_raw="NUMBER(5,2)",
                    pk=False,
                    notes="2024-04 以降のみ入る。それ以前は LEGACY_LAB を見る。" * 3,
                    score=0.9,
                )
                for j in range(loc_mod._TOP_K)
            ],
        )
        for i in range(DEFAULT_CHUNK_SIZE)
    ]

    user_msg = AILocationDrafter.__new__(AILocationDrafter)._build_user_message(contexts)
    schema = json.dumps(
        strict_response_format(LLMLocationChoices, *loc_mod._REQUIRED_CHOICE_KEYS),
        ensure_ascii=False,
    )
    total = (
        len(loc_mod.DRAFT_SYSTEM_PROMPT)
        + len(schema)
        + len(user_msg)
        + _OUTPUT_RESERVE_CHARS * DEFAULT_CHUNK_SIZE
    )
    num_ctx = int(LARGE_CONTEXT_OPTIONS["num_ctx"])
    assert total <= num_ctx, {
        "system": len(loc_mod.DRAFT_SYSTEM_PROMPT),
        "schema": len(schema),
        "user": len(user_msg),
        "total": total,
        "num_ctx": num_ctx,
    }
    # 既定（8192）では収まらないことも書いておく（`options` の配線が外れたら気づく）。
    assert total > int(DEFAULT_OPTIONS["num_ctx"])


def test_the_bm25_fallback_queries_and_shows_the_parent_text(monkeypatch):  # noqa: ANN001
    """BM25 へ縮退したときも親の text で引き、提示する。"""
    from kawasemi.ai_input import location_drafter as ld

    seen: list[str] = []

    class _Index:
        def search(self, query, top_k):  # noqa: ANN001, ANN202
            seen.append(query)
            return []

    monkeypatch.setattr(ld, "get_index", lambda *_a, **_k: _Index())
    drafter = ld.AILocationDrafter.__new__(ld.AILocationDrafter)
    drafter._corpus_dir = None
    contexts = drafter._retrieve(
        [{"link_id": "x", "item_text": "数値(mg/dL)", "parent_text": "CRP"}]
    )

    assert seen == ["CRP / 数値(mg/dL)"]
    assert "項目名: CRP / 数値(mg/dL)" in drafter._build_user_message(contexts)
