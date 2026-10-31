"""E2E 実機層: 決定的層と同じ資産・同じ関門を、bge-m3 と実際の LLM で通す。

`make e2e-live`（= `pytest tests/e2e/test_pipeline_live.py -m live`）で手元だけで回す。
既定の `pytest` では `live` marker を外してある（pyproject の addopts）。

差し込む物だけが決定的層と違う（`kawasemi/ai_input/e2e/pipeline.py`）:

- 索引と検索クエリの embedding: `EMBEDDING_MODEL`（bge-m3）を `EMBEDDING_BASE_URL` で
- 設計5段階（臨床・所在ヒント・所在・手順書・DSL）と実行時の llm_extract: `OLLAMA_MODEL`
- 手書き同意書の Vision: `OLLAMA_VISION_MODEL`。サーバに無ければ関門 (d) を
  `skipped: vision unavailable` にして表へ残す（黙って落とさない）

**関門 (b)〜(e) の合否は assert しない**（ダミーでの到達率を評価の物差しにしない）。
確かめるのは「工程が最後まで走り、全項目が終端（PASS か、最初に落ちた関門か、理由付き skip）
に着いた」ことだけで、表は `var/e2e/live_runs/<時刻>/run.{md,json}` に記録する。
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from kawasemi.ai_input.e2e.index import make_embedder
from kawasemi.ai_input.e2e.pipeline import (
    LIVE_RUNS_DIR,
    PipelineConfig,
    PipelineResult,
    ollama_models,
    run_pipeline,
    write_run_record,
)
from kawasemi.ai_input.e2e.scripted_llm import load_design_script
from kawasemi.core.config import settings
from tests.e2e.helpers import ollama_unavailable_reason

pytestmark = pytest.mark.live


def _embedding_unavailable_reason() -> str | None:
    models = ollama_models(settings.embedding_host)
    if not models:
        return f"embedding の Ollama へ到達できません（{settings.embedding_host}）"
    if not any(n.split(":")[0] == settings.embedding_model.split(":")[0] for n in models):
        return f"embedding モデル {settings.embedding_model!r} が {settings.embedding_host} にありません"
    return None


def _digest(models: dict[str, str], name: str) -> str:
    return models.get(name) or models.get(f"{name}:latest") or ""


def _knowledge(env: str) -> Path | None:
    """`draft-knowledge` から通すときの知識層（docs/e2e_guide.md §6）。未指定なら固定資産。"""
    given = os.environ.get(env, "").strip()
    return Path(given) if given else None


def _out_dir() -> Path:
    given = os.environ.get("E2E_LIVE_OUT", "").strip()
    if given:
        return Path(given)
    return LIVE_RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")


async def test_live_pipeline_runs_to_completion() -> None:
    reason = ollama_unavailable_reason(settings.ollama_model) or _embedding_unavailable_reason()
    if reason:
        pytest.skip(f"実機層を回せない: {reason}")

    llm_models = ollama_models(settings.ollama_base_url)
    emb_models = ollama_models(settings.embedding_host)
    vision_model = (settings.ollama_vision_model or "").strip()
    vision_available = bool(vision_model) and vision_model in llm_models
    out = _out_dir()
    knowledge_dwh = _knowledge("E2E_KNOWLEDGE_DWH")
    knowledge_billing = _knowledge("E2E_KNOWLEDGE_BILLING")
    meta: dict[str, Any] = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "out_dir": str(out),
        "vision_available": vision_available,
        "llm_total_timeout": settings.llm_total_timeout,
        "knowledge": {
            "dwh": str(knowledge_dwh or "examples/e2e/knowledge.yaml（固定資産）"),
            "billing": str(knowledge_billing or "examples/e2e/knowledge.billing.yaml（固定資産）"),
        },
        "models": {
            "設計・実行の LLM": {
                "name": settings.ollama_model,
                "digest": _digest(llm_models, settings.ollama_model),
                "base_url": settings.ollama_base_url,
            },
            "Vision": {
                "name": vision_model or None,
                "digest": _digest(llm_models, vision_model) if vision_model else "",
                "base_url": settings.ollama_base_url,
            },
            "embedding": {
                "name": settings.embedding_model,
                "digest": _digest(emb_models, settings.embedding_model),
                "base_url": settings.embedding_host,
            },
        },
    }

    result: PipelineResult | None = None
    t0 = time.perf_counter()
    try:
        result = await run_pipeline(
            PipelineConfig(
                work_dir=out,
                embedder=make_embedder("ollama", settings.embedding_model),
                script=load_design_script(),
                vision_available=vision_available,
                knowledge_dwh=knowledge_dwh,
                knowledge_billing=knowledge_billing,
            )
        )
    except Exception:
        meta["error"] = traceback.format_exc()
        raise
    finally:
        meta["elapsed_sec"] = round(time.perf_counter() - t0, 2)
        meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        js, md = write_run_record(out, meta, result)
        print(f"\n実機層の記録: {md}（{js}）")
        print(md.read_text("utf-8"))

    # 合否は記録だけ。確かめるのは「全項目が終端に着いた」こと
    assert result is not None
    assert set(result.report.items) == set(result.gold)
    assert result.report.unterminated() == [], result.report.table()
