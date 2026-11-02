# KAWASEMI 開発用 Makefile

# テストの並列実行の worker 数。既定 8。
# **`-n auto` にはしない**——16コアで 16 worker にすると、実 Ollama を使う2件
# （test_ai_step2_generate_runbooks / test_handwritten_consent_real_vision）が
# 同一サーバへ同時に投げられる密度が上がる。
# 壁時計の下限は最長の単一テスト（約58秒）なので、8 より増やしても縮まない。
# コア数の少ない環境では `make test WORKERS=4` のように上書きする。
WORKERS ?= 8

# テストは鍵の値に依存しない（CI と同じくテスト専用の値で足りる）。.env の SECRET_KEY /
# CSRF_SECRET_KEY が空のままでも `make test` / `make e2e` が通るよう、シェルの環境変数に
# 無ければテスト専用の値を渡す。環境変数は .env より優先されるので、テストの実行中は
# .env の鍵ではなくこの値が使われる（アプリの起動 `kawasemi serve` には効かない）。
TEST_KEYS = SECRET_KEY="$${SECRET_KEY:-make-test-only-secret-key-not-a-real-secret}" \
	CSRF_SECRET_KEY="$${CSRF_SECRET_KEY:-make-test-only-csrf-secret-key-not-a-real-secret}"

.PHONY: dsl-schema e2e e2e-live e2e-serve help hooks test

help: ## このヘルプを表示
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

test: ## テストを並列実行（既定 8 worker。`make test WORKERS=4` で変更）
	$(TEST_KEYS) uv run pytest -n $(WORKERS)

dsl-schema: ## DSL の step スキーマ（docs/schemas/dsl_step.schema.json）をコードから再生成
	uv run python scripts/export_dsl_schema.py

e2e: ## E2E 決定的層（関門 a〜e。Ollama・施設データ不要）
	$(TEST_KEYS) uv run pytest tests/e2e/test_pipeline_e2e.py -q

e2e-live: ## E2E 実機層（bge-m3 と LLM サーバ。結果は var/e2e/live_runs/ に記録）
	$(TEST_KEYS) uv run pytest tests/e2e/test_pipeline_live.py -m live -s -q -p no:cacheprovider

e2e-serve: ## E2E 実機層の最新の実行をブラウザで見る（127.0.0.1:8030）
	uv run kawasemi e2e serve

hooks: ## git フックを有効化（commit 時に ruff/mypy、push 時に main ガード）
	@# 旧方式（core.hooksPath = scripts/git-hooks）が残っていると pre-commit が
	@# .git/hooks/ へインストールできないため、先に解除する。
	@git config --unset core.hooksPath 2>/dev/null || true
	@uv run pre-commit install --hook-type pre-commit --hook-type pre-push
	@echo "✓ pre-commit / pre-push フックを有効化しました（緊急回避: git commit --no-verify / git push --no-verify）"
