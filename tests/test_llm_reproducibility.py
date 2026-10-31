"""LLM 出力の再現性の記録

文書とコード上の語彙は sha256 と値で固定できるが、**LLM の出力そのものは
固定できない**。`seed` をどこにも設定しておらず、`temperature: 0` だけでは Ollama で同一出力は
保証されない。モデルタグは環境変数で上書きでき、**`.env` を書き換えるだけで sha256 に一切
現れずモデルが変わる**。

ここでは (1) `seed` が両経路の options に入っていること、(2) 索引と埋め込みモデルの
食い違いが起動時に分かること、を固定する。**完全な再現は保証できない**
（テストで担保できる主張ではない）。
"""

from __future__ import annotations

import logging

import pytest

from kawasemi.ai_input import executor as executor_mod
from kawasemi.ai_input import llm_json as llm_json_mod
from kawasemi.ai_input.executor import _LLM_OPTIONS
from kawasemi.ai_input.llm_json import DEFAULT_OPTIONS


def test_the_execution_role_passes_a_seed() -> None:
    """実行ロール（`llm_extract` / `llm_judge` / Vision）が `seed` を渡すこと。"""
    assert "seed" in _LLM_OPTIONS
    assert _LLM_OPTIONS["temperature"] == 0, "前提: 温度0（seed はその上での決定性）"


def test_the_design_role_passes_a_seed() -> None:
    """設計ロール（下書き・手順書・コンパイル）が `seed` を渡すこと。

    `seed` は `options` に入れて渡す。
    """
    assert "seed" in DEFAULT_OPTIONS
    assert DEFAULT_OPTIONS["temperature"] == 0


def test_every_option_set_carries_the_same_seed() -> None:
    """**すべての options が同じ seed を持つこと**（経路ごとに違うと比較できない）。

    `LARGE_CONTEXT_OPTIONS`（大きい num_ctx の経路）だけ seed が無い、という
    形が起きると、**同じ運用の中で再現できる経路とできない経路が混ざる**。
    """
    # **モジュールを走査して見つける**（定数を列挙しない）。`LARGE_CONTEXT_OPTIONS` の
    # ように options が増えたとき、**列挙式だと新しい経路だけ seed が無い状態を見逃す**。
    seeds: dict[str, object] = {}
    for module in (executor_mod, llm_json_mod):
        for name in dir(module):
            if not name.endswith("OPTIONS"):
                continue
            value = getattr(module, name)
            if isinstance(value, dict) and "num_ctx" in value:
                seeds[f"{module.__name__}.{name}"] = value.get("seed")

    assert seeds, "LLM の options が1つも見つからない（走査の条件が実装とずれている）"
    assert None not in seeds.values(), f"seed の無い経路がある: {seeds}"
    assert len(set(seeds.values())) == 1, f"経路ごとに seed が違う: {seeds}"


def test_the_startup_check_is_quiet_without_an_index(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引が無い環境では**黙る**こと。

    CI・テスト・索引を使わない運用まで警告で埋めると、本当に食い違っているときの1行が
    埋もれる。
    """
    from kawasemi.api import main as main_mod

    class _NoIndexStore:
        build_info = None

    monkeypatch.setattr(
        "kawasemi.ai_input.resource_index.store.get_store", lambda _dir: _NoIndexStore()
    )

    with caplog.at_level(logging.INFO):
        main_mod._warn_if_the_index_disagrees()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_startup_check_reports_a_mismatch(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引と設定が食い違っていれば**起動時に**分かること。

    `embedding_model` が索引側と違うとベクトル空間が別物になり、**索引の検索そのものが
    無効化される**。典拠の付与率は「候補が1件も出ない」形で静かに 0 へ落ちるので、
    動かす前に分かる必要がある。
    """
    from kawasemi.api import main as main_mod

    class _BadBuildInfo:
        schema_version = 2
        embedding_model = "x"

    class _BadStore:
        build_info = _BadBuildInfo()
        schema_compatible = True

        def matches_model(self, model: str) -> bool:
            return model == "x"

    monkeypatch.setattr(
        "kawasemi.ai_input.resource_index.store.get_store", lambda _dir: _BadStore()
    )

    with caplog.at_level(logging.ERROR):
        main_mod._warn_if_the_index_disagrees()

    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("検索できません" in m for m in messages), messages
    assert any("embedding" in m for m in messages), messages
