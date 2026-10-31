# KAWASEMI

**K**nowledge-**A**ided **W**orkflow for **A**I-assisted **S**tructured **E**HR **M**apping & **I**nput

KAWASEMI helps fill clinical registry forms (FHIR Questionnaire / QuestionnaireResponse,
following the JASPEHR Implementation Guide) from hospital data. Forms can be entered by hand;
in AI mode, local LLMs (Ollama or vLLM) help design how each item is obtained — its clinical
meaning, where the data lives in the hospital's systems, a written retrieval procedure and an
executable DSL — and then run it against databases, files, FHIR servers and CSV exports.
It is designed to run entirely inside the facility network, with LLMs served locally.

> **Status: research software.** KAWASEMI is not a medical device and must not be used for
> clinical decision-making. It is provided as is, without warranty (see [LICENSE](LICENSE)).
> Never put patient data in issues, pull requests or test fixtures.

## Repository layout

| Path | Contents |
|---|---|
| `kawasemi/` | Web application (FastAPI), DSL compiler and executor |
| `indexer/` | `kawasemi-indexer`: builds the search index over a facility's data definitions (uv workspace member) |
| `examples/e2e/` | Fictional, self-contained scenario used by the end-to-end tests and the demo |
| `site.example/` | Templates for facility-specific configuration |
| `site/` | Facility-specific data and configuration (git-ignored; keep it in a separate private repository) |
| `var/` | Generated artifacts: index files, outputs (git-ignored) |
| `CONTRACT.md` | Data contract between the indexer and the web application |

## Quick start

```bash
uv sync --all-packages --all-groups --all-extras
cp .env.example .env
# SECRET_KEY and CSRF_SECRET_KEY are required (32+ characters each, different values);
# with the empty defaults every command stops with a settings validation error.
uv run python -c "import secrets; [print(f'{k}=\"{secrets.token_urlsafe(32)}\"') for k in ('SECRET_KEY', 'CSRF_SECRET_KEY')]" >> .env
uv run alembic upgrade head   # create the database schema (always required)
make e2e                      # deterministic end-to-end run on fictional data (no LLM needed)
uv run kawasemi serve         # web UI on http://localhost:8000/
```

`make test` and `make e2e` pass test-only keys when `SECRET_KEY` / `CSRF_SECRET_KEY` are not
set in the shell environment, so they also run before `.env` is filled in.

To use it at a facility, copy `site.example/` to `site/` (or point `SITE_DIR` at it) and fill in
the connection registry, key types and data definitions. The documentation below is in Japanese.

---

## 日本語

JASPEHR Implementation Guide に準拠した Questionnaire / QuestionnaireResponse テンプレート入力システムです。

## 概要

JASPEHR 標準の Web フォームとして症例データを手動入力できます。
**データ自動収集（AIモード）** では、電子カルテ・病理報告書などの構造化データやフリーテキストをローカル LLM（Ollama または vLLM）が解析し、各入力項目を自動補完します。

## はじめに

| ガイド | 内容 |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | ローカル起動・手動入力の基本操作 |
| [QUICKSTART_AI.md](QUICKSTART_AI.md) | データ自動収集（AIモード）を起動して動かすまで |
| [docs/ai_mode_guide.md](docs/ai_mode_guide.md) | データ自動収集（AIモード）のアーキテクチャ・各ステップの操作 |
| [docs/e2e_guide.md](docs/e2e_guide.md) | 自己完結のデモ（架空の様式とダミーデータで工程を通す E2E 試験環境） |
| [docs/dsl_reference.md](docs/dsl_reference.md) | DSL の op とその意味論（正本） |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 開発環境の構築・コードスタイル・マイグレーション |
| [docs/development_flow.md](docs/development_flow.md) | 開発の流れと検証手順 |

残作業はこのリポジトリの GitHub Issues で管理しています。脆弱性の報告は [SECURITY.md](SECURITY.md) に従ってください。

同梱している第三者のコードは [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) に記載しています。

## ライセンス

[MIT License](LICENSE)

