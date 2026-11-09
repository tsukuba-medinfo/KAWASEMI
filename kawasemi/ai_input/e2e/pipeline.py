"""E2E の一気通貫を1回走らせ、項目×関門の表を作る（決定的層と実機層で共有）。

様式 → 臨床的意味 → 所在 → 手順書 → DSL → 実行 → 回答の保存、を HTTP（ASGITransport）で通す。
二層の違いは**差し込む物だけ**で、工程と関門の判定はここに1本だけ持つ（docs/e2e_guide.md）。

| 差し込む物 | 決定的層（tests/e2e/test_pipeline_e2e.py） | 実機層（tests/e2e/test_pipeline_live.py） |
|---|---|---|
| 索引の embedder | `HashingEmbedder` | bge-m3（`OllamaEmbedder`） |
| 検索クエリの embedder | `HashingEmbedder` へ差し替え | 製品のまま（bge-m3） |
| 設計5段階と実行時の LLM | `ScriptedLLM` へ差し替え | 製品のまま（`OLLAMA_MODEL`） |
| Vision | 台本の文字起こし | `OLLAMA_VISION_MODEL`（無ければ (d) を理由付きで skip） |

**人の確認は設計者がする操作をそのまま自動化する。**

- 臨床的意味: 典拠つきの下書きが返った項目はそれを確認して保存する。返らなかった項目は、人が
  記入要領を読んで書いた記述（台本 `clinical.annotation`）を保存する
- 所在: 一推しを受け入れ、台本 `location.alternates`（第2候補）が**提示枠の中にあれば**足す
- 手順書: 生成されたものを確認済みにする。DSL: コンパイル結果を確認済みにする

FHIR は両層とも E2E の FHIR スタブ（`fhir_stub.py`）をプロセス内で使う（外部 FHIR へは出ない）。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import yaml
from httpx import ASGITransport, AsyncClient

from kawasemi.ai_input import e2e
from kawasemi.ai_input.dsl import has_needs_input
from kawasemi.ai_input.e2e import SUBJECTS, sources
from kawasemi.ai_input.e2e.gates import (
    FAIL,
    PASS,
    GateReport,
    answer_value,
    unit_matches,
    values_equal,
)
from kawasemi.ai_input.e2e.index import IndexBuildResult, build_e2e_index
from kawasemi.core.config import settings
from kawasemi.core.demo_secrets import demo_password

TOP_K = 8
QBASE = "/ai-input/questionnaires/{qid}"
# 外部 FHIR はプロセス内のスタブへ向ける（ネットワークに出ない架空のホスト名）
FHIR_BASE = "http://fhir-stub.e2e.invalid/fhir"
# 手書き PDF を Vision で読む項目（Vision が無い環境では (d) を理由付きで skip にする）
VISION_LINK_IDS = frozenset({"consent_date"})
VISION_UNAVAILABLE = "skipped: vision unavailable"
_CANDIDATE_KEYS = (
    "source_category",
    "location",
    "unit_id",
    "connection_id",
    "payload",
    "llm_model",
)


@dataclass
class PipelineResult:
    report: GateReport
    gold: dict[str, dict[str, Any]]
    programs: dict[str, dict[str, Any]]
    evidence: dict[str, dict[str, Any]]  # 患者 → evidence
    notes: dict[str, Any]
    timings: dict[str, float]  # 工程 → 秒
    questionnaire_id: int | None = None


@dataclass
class PipelineConfig:
    """1回の実行に差し込む物（モジュールの docstring の表）。

    `work_dir` に生成物・索引・アプリDB（`app.db`）・接続レジストリの複製を置く。
    決定的層は一時ディレクトリ、実機層は `var/e2e/live_runs/<時刻>/`。
    """

    work_dir: Path
    embedder: Any  # 索引の構築に使う
    script: dict[str, dict[str, Any]]  # 設計の台本（人の入力として臨床記述と第2候補を読む）
    llm: Any | None = None  # None なら製品の LLM client（実機）
    query_embedder: Callable[..., Any] | None = None  # None なら製品の OllamaEmbedder（実機）
    vision_available: bool = True
    top_k: int = TOP_K
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    # 確認済みの知識層（ingest の入力）。None なら固定資産（examples/e2e/knowledge*.yaml）
    knowledge_dwh: Path | None = None
    knowledge_billing: Path | None = None
    # 設計が済んで実行に入る直前に、生成物（`work_dir/generated`）を書き換える。決定的層が
    # 「1名の入院時サマリが無い」変種を作るのに使う。索引と設計には効かない
    before_execute: Callable[[Path], None] | None = None


class PipelineError(RuntimeError):
    """工程が続けられない（保存の API が失敗した等）。関門の失敗ではなく実行の失敗。"""


# --------------------------------------------------------------------------- #
# 差し替え
# --------------------------------------------------------------------------- #


def connections_yaml(out: Path, generated: Path, fhir_base: str = FHIR_BASE) -> Path:
    """接続レジストリを生成物へ向けた複製を `out` に作る（root_path を絶対パスへ）。"""
    data = yaml.safe_load(e2e.CONNECTIONS_E2E.read_text("utf-8"))
    conns = data["connections"]
    conns[e2e.CONN_FILES]["root_path"] = str((generated / e2e.REPORTS_DIR.name).resolve())
    conns[e2e.CONN_CSV]["root_path"] = str((generated / e2e.CSV_DIR.name).resolve())
    conns[e2e.CONN_FHIR]["base_url"] = fhir_base
    out.write_text(yaml.safe_dump(data, allow_unicode=True), "utf-8")
    return out


def _patch_fhir(stack: ExitStack) -> None:
    """`FHIRClient._get_resource` を E2E の FHIR スタブ（ASGI）へ向ける。"""
    from kawasemi.ai_input.e2e.fhir_stub import create_app
    from kawasemi.core.exceptions import FHIRServerException
    from kawasemi.utils.fhir_client import FHIRClient

    stub = create_app()

    async def _get(self: Any, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=ASGITransport(app=stub)) as c:
            r = await c.get(url, params=params)
        if r.status_code == 404:
            raise FHIRServerException(message="リソースが見つかりません", details={"url": url})
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    stack.enter_context(mock.patch.object(FHIRClient, "_get_resource", _get))


def _patch_llm(stack: ExitStack, llm: Any) -> None:
    """設計5段階と実行時の LLM を `llm` へ差し替える。"""
    from kawasemi.ai_input import (
        clinical_drafter,
        compiler,
        executor,
        location_drafter,
        resource_drafter,
        runbook,
    )
    from kawasemi.ai_input.llm_provider import provider_of

    for mod in (clinical_drafter, resource_drafter, location_drafter, runbook, compiler):
        stack.enter_context(mock.patch.object(mod, "make_design_llm_client", lambda: llm))

    original_init = executor.DSLInterpreter.__init__

    def _init(self: Any) -> None:
        original_init(self)
        self._ollama_client = llm
        self._provider = provider_of(llm)

    stack.enter_context(mock.patch.object(executor.DSLInterpreter, "__init__", _init))


def _patch_query_embedder(stack: ExitStack, factory: Callable[..., Any]) -> None:
    from kawasemi.ai_input import clinical_drafter, resource_drafter

    for mod in (clinical_drafter, resource_drafter):
        stack.enter_context(mock.patch.object(mod, "OllamaEmbedder", factory))


@contextmanager
def _timed(timings: dict[str, float], name: str) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round(time.perf_counter() - t0, 2)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

_CSRF_RE = re.compile(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']')


async def _login(client: AsyncClient, username: str, password: str) -> str:
    resp = await client.get("/login")
    m = _CSRF_RE.search(resp.text)
    if resp.status_code != 200 or not m:
        raise PipelineError(f"ログイン画面が開けない: {resp.status_code}")
    csrf = m.group(1)
    resp = await client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf},
        headers={"X-CSRF-Token": csrf},
    )
    if resp.status_code != 303:
        raise PipelineError(f"{username} でログインできない: {resp.status_code}")
    return csrf


async def _post(client: AsyncClient, url: str, csrf: str, body: Any = None) -> httpx.Response:
    return await client.post(url, json=body or {}, headers={"X-CSRF-Token": csrf})


UNATTEMPTED = (
    "未試行（全体の制限時間 LLM_TOTAL_TIMEOUT、または 1 コールの上限 OLLAMA_TIMEOUT による"
    "コンパイル打ち切りなど。押し直せば対象に入る）"
)
EMPTY_PROGRAM = "空のプログラム（step が無い）"
REJECTED_LLM = "LLM の応答を棄却（llm_extract / llm_judge を含むが自動構造化AIが無効）"


def compile_failure_reason(
    link_id: str, compiled: dict[str, Any], program: dict[str, Any] | None
) -> str | None:
    """`POST /plan/compile` の応答と保存されたプランから、関門 (c) で落ちる理由を返す。

    「未試行」（打ち切り。モデルが書けなかったのではない）・「棄却」・「空のプログラム」を
    「プランが返らなかった」や検証エラーと分けて書く。落ちないなら None。
    """
    if link_id in (compiled.get("unattempted_link_ids") or []):
        return UNATTEMPTED
    if link_id in (compiled.get("rejected_llm") or []):
        return REJECTED_LLM
    if program is None:
        return "プランが返らなかった"
    if link_id in (compiled.get("empty_program_link_ids") or []):
        return EMPTY_PROGRAM
    errors = (compiled.get("validation_errors") or {}).get(link_id)
    if errors:
        return f"検証エラー: {errors}"
    if has_needs_input(program):
        why = [st.get("reasoning") for st in program.get("steps", []) if st.get("needs_input")]
        return f"needs_input が残った: {why}"
    return None


def _fail_open(report: GateReport, gate: str, reason: str) -> None:
    """工程そのものが落ちたとき、その関門に来ている項目を全部 FAIL にする。"""
    for it in report.items.values():
        if it.open_at(gate) and it.gates[gate] not in (PASS, FAIL):
            it.set(gate, False, reason)


# --------------------------------------------------------------------------- #
# 本体
# --------------------------------------------------------------------------- #


async def run_pipeline(cfg: PipelineConfig) -> PipelineResult:
    """生成 → 索引 → 設計 → 実行 → 保存を1回通し、関門 (a)〜(e) を判定する。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from kawasemi.ai_input import reference_scope
    from kawasemi.ai_input.e2e.seed import USER_COLLECTOR, USER_DESIGNER, seed_all
    from kawasemi.ai_input.models import InputPlan, InputRunbook
    from kawasemi.ai_input.resource_index import connections as conn_mod
    from kawasemi.ai_input.resource_index import key_types as kt_mod
    from kawasemi.ai_input.resource_index.store import get_store, reset_store_cache
    from kawasemi.ai_input.routes import plan_version, runbook_version
    from kawasemi.api.main import app
    from kawasemi.core.database import get_db
    from kawasemi.core.entry import reset_entry_cache
    from kawasemi.core.rate_limit import limiter
    from kawasemi.models import User
    from kawasemi.models.base import Base
    from kawasemi.models.case_assignment import CaseAssignment
    from kawasemi.models.questionnaire_response import QuestionnaireResponse

    work = cfg.work_dir
    work.mkdir(parents=True, exist_ok=True)
    generated = work / "generated"
    script = cfg.script
    gold = {g["link_id"]: g for g in sources.build_gold()}
    report = GateReport()
    for lid, g in gold.items():
        report.add(lid, g["route"])
    programs: dict[str, dict[str, Any]] = {}
    evidence_by_patient: dict[str, dict[str, Any]] = {}
    notes: dict[str, Any] = {}
    timings: dict[str, float] = {}
    top_k = cfg.top_k

    # ---- 生成と索引（関門 a） -----------------------------------------------------
    with _timed(timings, "generate"):
        sources.generate_all(generated, gold_path=None)
    with _timed(timings, "index"):
        built: IndexBuildResult = build_e2e_index(
            work / "resource_index",
            cfg.embedder,
            work_dir=work / "index_work",
            generated_dir=generated,
            knowledge_dwh=cfg.knowledge_dwh or e2e.KNOWLEDGE_DWH,
            knowledge_billing=cfg.knowledge_billing or e2e.KNOWLEDGE_BILLING,
        )
    notes["index"] = {
        "unit_count": built.unit_count,
        "item_units": list(built.item_units),
        "physical_units": built.physical_units,
        "index_dir": str(built.index_dir),
    }

    db_path = work / "app.db"
    if db_path.exists():
        db_path.unlink()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    overrides: dict[str, Any] = {
        "enable_ai_drafting": True,
        "enable_ai_extraction": True,
        "llm_provider": "ollama",
        "resource_index_dir": str(built.index_dir),
        "connections_file": str(connections_yaml(work / "connections.yaml", generated)),
        "key_types_file": str(e2e.KEY_TYPES_E2E.resolve()),
        "reference_scopes_file": str(e2e.REFERENCE_SCOPES.resolve()),
        "fhir_server_url": FHIR_BASE,
        "trial_run_patient_ids": "",
        "entry_name": "",
        "entry_roles": "",
        "entry_urls": "",
        **cfg.settings_overrides,
    }
    notes["connections_file"] = overrides["connections_file"]
    env = {
        "DWH_DSN": f"sqlite+aiosqlite:///{(generated / e2e.WAREHOUSE_DB.name).resolve()}",
        "BILLING_DSN": f"sqlite+aiosqlite:///{(generated / e2e.BILLING_DB.name).resolve()}",
        "FHIR_STUB_DIR": str((generated / e2e.FHIR_STUB_DIR.name).resolve()),
    }

    def _reset_caches() -> None:
        reset_entry_cache()
        reset_store_cache()
        reference_scope.reset_reference_scope_cache()
        conn_mod._cache.clear()
        kt_mod._cache.clear()

    qid: int | None = None
    with ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env))
        for name, value in overrides.items():
            stack.enter_context(mock.patch.object(settings, name, value))
        _reset_caches()
        stack.callback(_reset_caches)
        limiter.reset()
        _patch_fhir(stack)
        if cfg.llm is not None:
            _patch_llm(stack, cfg.llm)
        if cfg.query_embedder is not None:
            _patch_query_embedder(stack, cfg.query_embedder)
        app.dependency_overrides[get_db] = _override_get_db
        stack.callback(app.dependency_overrides.pop, get_db, None)

        store = get_store()
        index_ids = [u.unit_id for u in store.units]
        for lid, g in gold.items():
            hit = [u for u in index_ids if unit_matches(g["unit_id"], u)]
            report.items[lid].set(
                "a", bool(hit), *(() if hit else (f"{g['unit_id']} が索引に無い",))
            )

        qid = await seed_all(session_factory)
        q = QBASE.format(qid=qid)
        async with session_factory() as s:
            collector = (
                await s.execute(select(User).where(User.username == USER_COLLECTOR))
            ).scalar_one()
            for subj in SUBJECTS:
                s.add(
                    CaseAssignment(
                        questionnaire_id=qid, patient_id=subj.reg_no, assigned_to_id=collector.id
                    )
                )
            await s.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
        ) as client:
            csrf = await _login(client, USER_DESIGNER, demo_password())
            link_ids = list(gold)

            # ---- 臨床的意味（典拠つきの下書き → 人が確認） ----------------------------
            with _timed(timings, "clinical"):
                r = await _post(
                    client, f"{q}/source-survey/clinical/ai-draft", csrf, {"link_ids": link_ids}
                )
            drafts = (
                {d["link_id"]: d for d in r.json().get("drafts", [])}
                if r.status_code == 200
                else {}
            )
            notes["clinical_draft"] = {
                "status": r.status_code,
                "with_citation": sorted(lid for lid, d in drafts.items() if d.get("citation")),
            }
            cited: dict[str, dict[str, Any]] = {}
            for lid, d in drafts.items():
                if not d.get("citation"):
                    continue
                uid = d["citation"]["unit_id"]
                unit = store.get_unit(uid)
                cited[lid] = {
                    "unit_id": uid,
                    "cites_section": bool(
                        unit and script[lid]["clinical"]["cite"] in unit.search_text
                    ),
                }
            notes["clinical_citations"] = cited
            clinical_items = []
            for lid in link_ids:
                d = drafts.get(lid) or {}
                if d.get("citation") and d.get("annotation"):
                    clinical_items.append(
                        {
                            "link_id": lid,
                            "annotation": d["annotation"],
                            "citations": [d["citation"]],
                            "llm_model": d["citation"].get("llm_model"),
                        }
                    )
                else:
                    # 下書きが返らなかった項目は、人が記入要領を読んで書く（台本の同じ文）
                    clinical_items.append(
                        {"link_id": lid, "annotation": script[lid]["clinical"]["annotation"]}
                    )
            r = await client.post(
                f"{q}/source-survey/clinical?confirm=true",
                json={"items": clinical_items},
                headers={"X-CSRF-Token": csrf},
            )
            if r.status_code != 200:
                raise PipelineError(f"臨床的意味を保存できない: {r.status_code} {r.text[:300]}")

            # ---- 所在（索引検索 → 一推し → 人が確認）: 関門 b ---------------------------
            with _timed(timings, "location"):
                r = await _post(
                    client, f"{q}/source-survey/system/ai-draft", csrf, {"link_ids": link_ids}
                )
            if r.status_code != 200:
                _fail_open(report, "b", f"所在のAI案が {r.status_code}: {r.text[:200]}")
            loc_drafts = (
                {d["link_id"]: d for d in r.json().get("drafts", [])}
                if r.status_code == 200
                else {}
            )
            response_rank: dict[str, int | None] = {}
            system_items: list[dict[str, Any]] = [
                {"link_id": "patient_id", "candidates": [{"source_category": "not_available"}]}
            ]
            for lid, g in gold.items():
                it = report.items[lid]
                if not it.open_at("b") or lid not in loc_drafts:
                    if it.open_at("b"):
                        it.set("b", False, "所在のAI案が返らなかった")
                    system_items.append(
                        {"link_id": lid, "candidates": [{"source_category": "not_available"}]}
                    )
                    continue
                cands = [c for c in loc_drafts[lid]["candidates"] if not c.get("fallback")][:top_k]
                uids = [c.get("unit_id") or "" for c in cands]
                rank = next((i for i, u in enumerate(uids) if unit_matches(g["unit_id"], u)), None)
                response_rank[lid] = rank
                rec = next((c for c in cands if c.get("recommended")), None)
                reasons = []
                if rank is None:
                    reasons.append(f"{g['unit_id']} が上位{top_k}に無い（上位: {uids[:3]}）")
                elif rec is None or not unit_matches(g["unit_id"], rec.get("unit_id") or ""):
                    reasons.append(f"一推しが期待単位でない: {rec and rec.get('unit_id')}")
                chosen = [rec] if rec else []
                for alt in script[lid]["location"].get("alternates") or []:
                    c = next((c for c in cands if c.get("unit_id") == alt), None)
                    if c is None:
                        reasons.append(f"第2候補 {alt} が上位{top_k}に無い")
                    else:
                        chosen.append(c)
                it.set("b", not reasons, *reasons)
                if reasons:
                    system_items.append(
                        {"link_id": lid, "candidates": [{"source_category": "not_available"}]}
                    )
                else:
                    system_items.append(
                        {
                            "link_id": lid,
                            "candidates": [{k: c.get(k) for k in _CANDIDATE_KEYS} for c in chosen],
                        }
                    )
            notes["location_response_rank"] = response_rank
            r = await client.post(
                f"{q}/source-survey/system?confirm=true",
                json={"items": system_items},
                headers={"X-CSRF-Token": csrf},
            )
            if r.status_code != 200:
                raise PipelineError(f"所在を保存できない: {r.status_code} {r.text[:300]}")

            # ---- 手順書 ----------------------------------------------------------------
            with _timed(timings, "runbook"):
                r = await _post(client, f"{q}/runbook/generate", csrf, {"model": None})
            if r.status_code != 200:
                _fail_open(report, "c", f"手順書の生成が {r.status_code}: {r.text[:200]}")
            async with session_factory() as s:
                rbs = {
                    rb.link_id: rb
                    for rb in (
                        await s.execute(
                            select(InputRunbook).where(InputRunbook.questionnaire_id == qid)
                        )
                    ).scalars()
                }
            for lid, it in report.items.items():
                if not it.open_at("c") or it.gates["c"] == FAIL:
                    continue
                rb = rbs.get(lid)
                if rb is None or not rb.steps:
                    it.set("c", False, "手順書が生成されなかった")
                    continue
                # 確認には画面と同じく「見た版」を添える（routes.runbook_version）
                r = await client.post(
                    f"{q}/runbook/{lid}?confirm=true",
                    json={"steps": rb.steps, "base_version": runbook_version(rb)},
                    headers={"X-CSRF-Token": csrf},
                )
                if r.status_code != 200:
                    it.set("c", False, f"手順書を確認できない: {r.text[:200]}")

            # ---- DSL のコンパイル → 確認済み: 関門 c -------------------------------------
            with _timed(timings, "compile"):
                r = await _post(client, f"{q}/plan/compile", csrf, {"model": None})
            compiled = r.json() if r.status_code == 200 else {}
            if r.status_code != 200:
                _fail_open(report, "c", f"コンパイルが {r.status_code}: {r.text[:200]}")
            async with session_factory() as s:
                plans = {
                    p.link_id: p
                    for p in (
                        await s.execute(select(InputPlan).where(InputPlan.questionnaire_id == qid))
                    ).scalars()
                }
            for lid, it in report.items.items():
                if not it.open_at("c") or it.gates["c"] == FAIL:
                    continue
                plan = plans.get(lid)
                if plan is not None:
                    programs[lid] = plan.program
                why_not = compile_failure_reason(
                    lid, compiled, plan.program if plan is not None else None
                )
                if why_not:
                    it.set("c", False, why_not)
                    continue
                assert plan is not None
                r = await client.post(
                    f"{q}/plan/{lid}?confirm=true",
                    json={"program": plan.program, "base_version": plan_version(plan)},
                    headers={"X-CSRF-Token": csrf},
                )
                it.set(
                    "c",
                    r.status_code == 200 and r.json().get("status") == "confirmed",
                    *(() if r.status_code == 200 else (f"確認できない: {r.text[:200]}",)),
                )
            await client.get("/logout")

            # ---- 実行（収集者の本番 autofill）→ 回答の送信: 関門 d / e ------------------
            if cfg.before_execute is not None:
                cfg.before_execute(generated)
            csrf = await _login(client, USER_COLLECTOR, demo_password())
            mismatch_d: dict[str, list[str]] = {lid: [] for lid in gold}
            mismatch_e: dict[str, list[str]] = {lid: [] for lid in gold}
            autofill_errors: list[str] = []  # autofill そのものが失敗した患者（Vision と無関係）
            t_exec = time.perf_counter()
            for subj in SUBJECTS:
                reg = subj.reg_no
                r = await _post(client, f"{q}/autofill", csrf, {"patient_id": reg})
                if r.status_code != 200 or not r.json().get("success"):
                    autofill_errors.append(reg)
                    for lid in gold:
                        mismatch_d[lid].append(f"{reg}: autofill {r.status_code} {r.text[:120]}")
                    continue
                body = r.json()
                prefill, ev = body["prefill_data"], body["evidence"]
                evidence_by_patient[reg] = ev
                for lid, g in gold.items():
                    if not report.items[lid].open_at("d"):
                        continue
                    got = prefill.get(lid)
                    if not values_equal(g["expected"][reg], got):
                        mismatch_d[lid].append(
                            f"{reg}: 期待 {g['expected'][reg]!r} / 実行 {got!r}"
                            f"（{(ev.get(lid) or {}).get('failure_code')}）"
                        )
                    src = (g.get("expected_source") or {}).get(reg)
                    if src:
                        # 本文読み（llm_extract）の step が走ったか。step の id は台本と実機で
                        # 違うので op で見る（guard で飛ばされた step は skipped=True）
                        text_ran = any(
                            t.get("op") == "llm_extract" and t.get("skipped") is not True
                            for t in (ev.get(lid) or {}).get("trace", [])
                        )
                        if (src == "text") != text_ran:
                            mismatch_d[lid].append(
                                f"{reg}: 経路が {src} のはずが本文読みの実行={text_ran}"
                            )
                # 回答の送信（フォームの確定保存と同じ経路）
                form = {"subject": f"Patient/{reg}", "csrf_token": csrf, "patient_id": reg}
                form.update({k: str(v) for k, v in prefill.items() if v is not None})
                r = await client.post(
                    f"/forms/{qid}/submit", data=form, headers={"X-CSRF-Token": csrf}
                )
                async with session_factory() as s:
                    qr = (
                        await s.execute(
                            select(QuestionnaireResponse).where(
                                QuestionnaireResponse.subject == f"Patient/{reg}"
                            )
                        )
                    ).scalar_one_or_none()
                answers = (
                    {i["linkId"]: answer_value(i) for i in qr.content.get("item", [])} if qr else {}
                )
                for lid, g in gold.items():
                    if qr is None:
                        mismatch_e[lid].append(f"{reg}: 回答が保存されない（{r.status_code}）")
                    elif not values_equal(g["expected"][reg], answers.get(lid)):
                        mismatch_e[lid].append(
                            f"{reg}: 期待 {g['expected'][reg]!r} / 保存 {answers.get(lid)!r}"
                        )
            timings["execute"] = round(time.perf_counter() - t_exec, 2)
            # 関門 (e) は (d) で落ちた項目を判定しない（skip）。保存値の食い違いそのものは
            # 残しておく（入院時サマリが無い変種で「保存されなかったのは誰か」を見るため）
            notes["mismatch_e"] = {lid: list(v) for lid, v in mismatch_e.items() if v}
            for lid, it in report.items.items():
                if it.open_at("d"):
                    # Vision が無いことで判定できなかったときだけ skip。autofill 自体が
                    # 落ちた患者がいれば、それは Vision と無関係の失敗として残す
                    if lid in VISION_LINK_IDS and not cfg.vision_available and not autofill_errors:
                        it.skip("d", VISION_UNAVAILABLE)
                    else:
                        it.set("d", not mismatch_d[lid], *mismatch_d[lid])
                if it.open_at("e"):
                    it.set("e", not mismatch_e[lid], *mismatch_e[lid])
            await client.get("/logout")
    await engine.dispose()

    notes["timings"] = timings
    return PipelineResult(
        report=report,
        gold=gold,
        programs=programs,
        evidence=evidence_by_patient,
        notes=notes,
        timings=timings,
        questionnaire_id=qid,
    )


# --------------------------------------------------------------------------- #
# 実機層の補助
# --------------------------------------------------------------------------- #


def ollama_models(base_url: str, timeout: float = 5.0) -> dict[str, str]:
    """Ollama の `/api/tags` からモデル名 → digest を返す。取れなければ空。"""
    try:
        r = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        r.raise_for_status()
    except Exception:
        return {}
    return {
        str(m.get("name", "")): str(m.get("digest", ""))
        for m in r.json().get("models", [])
        if m.get("name")
    }


# 実機層の出力先（生成物の側。gitignore）。1回ごとに `<時刻>/` を切る
LIVE_RUNS_DIR = e2e.GENERATED_DIR / "live_runs"

_STAGE_LABELS = {
    "generate": "生成",
    "index": "索引（ingest → build → 項目単位）",
    "clinical": "臨床的意味の下書き",
    "location": "所在の下書き（ヒント＋検索＋一推し）",
    "runbook": "手順書の生成",
    "compile": "DSL のコンパイル",
    "execute": "実行（10名の autofill と回答の送信）",
}


def latest_live_run(root: Path = LIVE_RUNS_DIR) -> Path | None:
    """いちばん新しい実機層の出力（`app.db` があるもの）。無ければ None。"""
    runs = sorted(p for p in root.glob("*/") if (p / "app.db").exists()) if root.exists() else []
    return runs[-1] if runs else None


def run_record_markdown(meta: dict[str, Any], result: PipelineResult | None) -> str:
    """実機層の1回分の記録（Markdown）。**評価の数字ではなく「通るか」の記録**（ダミーでの到達率を評価の物差しにしない）。"""
    lines = [
        f"# E2E 実機層の記録（{meta.get('started_at', '')}）",
        "",
        "ダミーデータでの到達の記録であり、評価の指標ではない。",
        "",
        "## モデル",
        "",
        "| 役割 | モデル | digest | 当て先 |",
        "|---|---|---|---|",
    ]
    for role, m in (meta.get("models") or {}).items():
        lines.append(
            f"| {role} | {m.get('name') or '-'} | {(m.get('digest') or '-')[:12]} | "
            f"{m.get('base_url') or '-'} |"
        )
    vision = meta.get("vision_available")
    lines += ["", f"- Vision: {'使える' if vision else '使えない（関門 d を理由付きで skip）'}"]
    if meta.get("llm_total_timeout") is not None:
        lines.append(f"- 全体の制限時間（LLM_TOTAL_TIMEOUT）: {meta['llm_total_timeout']} 秒")
    if meta.get("ollama_timeout") is not None:
        lines.append(f"- 1 コールの上限（OLLAMA_TIMEOUT）: {meta['ollama_timeout']} 秒")
    for name, path in (meta.get("knowledge") or {}).items():
        lines.append(f"- 知識層（{name}）: {path}")
    if meta.get("elapsed_sec") is not None:
        lines.append(
            f"- 全体の所要: {meta['elapsed_sec']} 秒（{meta.get('started_at')} 〜 {meta.get('finished_at')}）"
        )
    if meta.get("error"):
        lines += ["", "## 実行が止まった", "", "```", str(meta["error"]), "```"]
    if result is not None:
        lines += ["", "## 所要時間", "", "| 工程 | 秒 |", "|---|---|"]
        for k, v in result.timings.items():
            lines.append(f"| {_STAGE_LABELS.get(k, k)} | {v} |")
        lines.append(f"| 計 | {round(sum(result.timings.values()), 2)} |")
        lines += ["", "## 項目×関門", "", result.report.table()]
        clinical = result.notes.get("clinical_draft") or {}
        lines += [
            "",
            "## 補足",
            "",
            f"- 典拠つきの臨床下書きが返った項目: {clinical.get('with_citation')}",
            f"- 所在の応答での期待単位の位置（0始まり。一推しは先頭へ繰り上がる）: "
            f"{result.notes.get('location_response_rank')}",
            f"- 索引: {(result.notes.get('index') or {}).get('unit_count')} 単位",
        ]
    return "\n".join(lines) + "\n"


def write_run_record(
    out_dir: Path, meta: dict[str, Any], result: PipelineResult | None
) -> tuple[Path, Path]:
    """`run.json`（関門・メタ・プログラム・evidence）と `run.md`（表）を書く。"""
    import json

    out_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"meta": meta}
    if result is not None:
        payload.update(
            {
                "gates": result.report.to_json(),
                "timings": result.timings,
                "notes": result.notes,
                "programs": result.programs,
                "evidence": result.evidence,
            }
        )
    js = out_dir / "run.json"
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), "utf-8")
    md = out_dir / "run.md"
    md.write_text(run_record_markdown(meta, result), "utf-8")
    return js, md
