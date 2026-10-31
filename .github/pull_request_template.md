<!-- タイトルは Conventional Commits 形式で: feat|fix|refactor|style|test|docs|chore(scope): 要約 -->
<!-- 手順は docs/development_flow.md を参照 -->

## 概要
<!-- 何を・なぜ変更したか。1〜3行で。 -->

## 関連 issue
<!-- 受け入れ条件をすべて満たすなら Closes、一部なら Refs。 -->
Closes #

## 変更内容
-

## テスト / 確認
<!-- 実行したコマンドと結果を残す。 -->
- [ ] `uv run pytest` が green
- [ ] `uv run ruff check .` / `uv run ruff format --check .` が pass
- [ ] `uv run mypy kawasemi/ scripts/` と `(cd indexer && uv run mypy kawasemi_indexer/)` が pass
- [ ] `make e2e` が pass

**どこで回したか**: <!-- ローカル / GitHub Actions -->

**揃っていた依存**: <!-- ENABLE_AI_DRAFTING と ENABLE_AI_EXTRACTION、実 Ollama の有無。依存ごとの skip 範囲は development_flow.md 4.3 の表を参照 -->

**skip / 既知の失敗の切り分け**: <!-- 4.4 の切り分けを行った場合、その判断。--deselect した場合はその旨。skip が0件なら「skip なし」。 -->

## 受け入れ条件への対応
<!-- issue の受け入れ条件を1つずつ引き、何をもって満たしたかを書く。 -->
-

## レビュー観点 / 補足
<!-- 重点的に見てほしい箇所、既知の制約、フォロー予定。 -->
