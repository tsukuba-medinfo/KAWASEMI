# KAWASEMI

**English** | [日本語](README.md)

**K**nowledge-**A**ided **W**orkflow for **A**I-assisted **S**tructured **E**HR **M**apping & **I**nput

KAWASEMI helps fill clinical registry forms (FHIR Questionnaire / QuestionnaireResponse) from
hospital data. It aims at conformance to the JASPEHR Implementation Guide; full conformance is
not claimed (see [docs/jaspehr_compliance.md](docs/jaspehr_compliance.md), in Japanese).

Forms can be entered by hand. In AI mode, local LLMs (Ollama or vLLM) help design how each item
is obtained: its clinical meaning, where the data lives in the hospital's systems, a written
retrieval procedure, and an executable DSL converted from that procedure. A human confirms the
result, and only the confirmed DSL is run against databases, files, FHIR servers and CSV exports.
The AI writes drafts; a human confirms. Every value keeps its evidence (the documents read, the
SQL, the LLM prompts). It is designed to run entirely inside the facility network, with LLMs
served locally.

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
the connection registry, key types and data definitions.

## Documentation

Most documentation is written in Japanese.

| Document | Contents |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | Run locally and enter data by hand |
| [QUICKSTART_AI.md](QUICKSTART_AI.md) | Start and run data auto-collection (AI mode) |
| [docs/ai_mode_guide.md](docs/ai_mode_guide.md) | Architecture and steps of AI mode |
| [docs/e2e_guide.md](docs/e2e_guide.md) | Self-contained demo: an end-to-end run on a fictional form and dummy data |
| [docs/dsl_specification.md](docs/dsl_specification.md) | DSL specification (Japanese, with an English summary). Normative |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Development setup, code style, migrations |
| [docs/development_flow.md](docs/development_flow.md) | Development flow and verification |

The machine-readable step schema of the DSL is [docs/schemas/dsl_step.schema.json](docs/schemas/dsl_step.schema.json).

## Other

- Open work is tracked in this repository's GitHub Issues.
- Report vulnerabilities as described in [SECURITY.md](SECURITY.md).
- Bundled third-party code is listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

[Apache License 2.0](LICENSE) (attribution in [NOTICE](NOTICE))
