"""kawasemi-indexer CLI。

サブコマンド構成:
  ingest          確認済知識層 YAML → column_index.jsonl 一式を生成
  lint            定義書（Excel/CSV）を検品し qa レポートを出力（全角混入・区切り誤り等）
  extract-pdf-doc 紙・スキャンPDF定義書からカラム定義を復元し正準CSVを出力（vision OCR）
  draft-key-types 患者キー列に key_type 候補を提案（--from-live でフォーマット指紋補強）
  draft-knowledge information_schema＋定義書から知識層 YAML を下書き（LLM）
  build           column_index.jsonl → embedding 索引（embeddings.npy 等）
  enrich-physical 既存の索引の db 単位へ実行用の識別子（payload.physical）を追記
  build-synonyms  master_index.jsonl → 標準コード変換テーブル（クエリ展開用・任意）
  draft-ref-links 列→参照先マスタのリンクを下書き（参照リンクチャネル用・要人手確認）
  search          索引の点検（NL → 候補ランキング表示。字面＋embedding ハイブリッド）
  eval            ゴールドセットで検索精度を測る（recall@k / MRR。モード別比較）
  debug           対話的な索引診断コンソール（臨床的コンテキスト → データの場所）
  serve           索引診断 WebUI/JSON API をローカル起動（debug の Web 版・要 webui extra。Bearer トークン必須）

search は「ビルドした索引の点検」用の最小実装（store＋embedder）。本番 WebUI 側の
BM25 フォールバックや location 導出（derive）は indexer の責務外なので持たない。
debug は search の対話版＋直接内省（docs/kawasemi_indexer_debug_spec.md）。
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from pathlib import Path

from kawasemi_indexer import __version__
from kawasemi_indexer.config import settings
from kawasemi_indexer.logging_config import setup_logging


def cmd_build(args: argparse.Namespace) -> int:
    """column_index.jsonl → embedding 索引を生成する。"""
    from kawasemi_indexer.builder import build_index
    from kawasemi_indexer.embedder import OllamaEmbedder

    db_source = args.db_source
    if not any([db_source, args.fhir_source, args.file_source, args.file_manifest]):
        db_source = settings.db_definition_dir

    embedder = OllamaEmbedder(
        model=args.model, base_url=settings.embedding_host, timeout=settings.ollama_timeout
    )
    print(
        f"リソース索引を生成します: db={db_source or '-'}, fhir={args.fhir_source or '-'}, "
        f"file={args.file_source or '-'}, out={args.out}, model={args.model}"
    )
    info = build_index(
        args.out,
        embedder,
        db_source=db_source,
        fhir_source=args.fhir_source,
        file_source=args.file_source,
        file_manifest=args.file_manifest,
    )
    print(
        f"完了: {args.out} に {info.unit_count} ユニット"
        f"（{info.kind_counts}, dim={info.dim}, model={info.embedding_model}）を書き出しました"
    )
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """確認済知識層 YAML → column_index.jsonl 一式を生成する。"""
    from kawasemi_indexer.ingestion.pipeline import ingest_connection

    print(f"取り込み開始: knowledge={args.knowledge}, out={args.out}")
    result = ingest_connection(
        args.knowledge,
        args.out,
        dsn=args.dsn,
        schema=args.schema,
        lint_fix=args.fix,
        force=args.force,
        catalog_path=args.catalog,
    )
    print(
        f"完了: connection={result.connection_id} "
        f"{result.column_count}カラム / {result.table_count}表 / "
        f"コード辞書{result.code_entry_count}件"
    )
    print(
        f"  補完欠落（論理名なし）={result.unenriched_count} / "
        f"棄却定義書行（実DBに無い）={result.discarded_doc_rows}"
    )
    if result.drift and result.drift.has_changes:
        print(f"  ⚠ drift {len(result.drift.items)}件（要再確認）")
    print(f"次の手順: kawasemi-indexer build --db-source {args.out}")
    return 0


def cmd_enrich_physical(args: argparse.Namespace) -> int:
    """既存の索引へ payload.physical を追記する（search_text・embeddings は変えない）。"""
    from kawasemi_indexer.physical import enrich_physical, load_groups, read_catalog

    groups = load_groups(args.knowledge)
    if not groups.schemas and not groups.doc_headers:
        print(
            "知識層に physical_schemas も doc_header も無い（付けられる単位が無い）",
            file=sys.stderr,
        )
        return 1
    result = enrich_physical(args.base, args.out, groups, read_catalog(args.catalog))
    print(
        f"db 単位 {result.units}: physical を付けた {result.added} / "
        f"一意に決まらず未付与 {result.unresolved}（うち古い physical を外した {result.removed}）/ "
        f"文書種別の access_path {result.access_paths} → {args.out}"
    )
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """定義書を検品し、qa_report.md / qa_report.jsonl を出力する。"""
    from kawasemi_indexer.ingestion.knowledge import load_knowledge
    from kawasemi_indexer.ingestion.lint import (
        lint_tabular,
        merge_reports,
        write_reports,
    )
    from kawasemi_indexer.ingestion.tabular import read_tabular

    knowledge = load_knowledge(args.knowledge)
    if not knowledge.tabular_sources:
        print(f"検品対象の定義書がありません（knowledge.tabular_sources 空）: {args.knowledge}")
        return 0
    reports = [
        lint_tabular(read_tabular(src, normalize=args.fix)) for src in knowledge.tabular_sources
    ]
    report = merge_reports(reports)
    md, jsonl = write_reports(report, args.out)
    print(f"検品完了: {report.summary()}")
    print(f"  レポート: {md} / {jsonl}")
    if report.has_errors:
        print(
            f"  ⚠ error が {report.count('error')} 件あります（ingest は --fix か --force が必要）"
        )
        return 1
    return 0


def cmd_extract_pdf_doc(args: argparse.Namespace) -> int:
    """紙・スキャンPDF定義書からカラム定義を復元し、正準 CSV を書き出す。"""
    import asyncio as _asyncio
    from pathlib import Path

    from kawasemi_indexer.ingestion.pdf_doc import (
        extract_records,
        read_pdf_pages,
        write_doc_csv,
        write_extraction_report,
    )

    model = args.model or settings.ollama_model
    print(f"PDF を読み込み中: {args.pdf}")
    pages = read_pdf_pages(args.pdf, render_scanned=not args.no_ocr)
    extraction = _asyncio.run(extract_records(pages, model=model, vision_model=args.vision_model))
    csv_path = write_doc_csv(extraction.records, args.out)
    report_path = Path(args.out).with_suffix(".report.json")
    write_extraction_report(extraction, report_path)

    failed = [p for p in extraction.pages if p.method == "failed"]
    print(
        f"完了: {len(extraction.records)} カラム定義を {csv_path} に書き出しました"
        f"（ページ{len(extraction.pages)} / OCR不能{len(failed)}）"
    )
    print(f"  レポート: {report_path}")
    print(
        "  ※ OCR/LLM 由来の下書きです。CSV を人が確認・修正してから draft-knowledge/ingest --doc へ。"
    )
    if extraction.has_patient_data_risk:
        for f in extraction.guard_findings:
            print(f"  ⚠ 患者データ誤投入ガード: {f}")
        return 1
    return 0


def cmd_draft_key_types(args: argparse.Namespace) -> int:
    """索引の患者キー列に key_type 候補を提案する（--from-live で指紋補強）。"""
    import json as _json

    from kawasemi_indexer.key_type_drafter import KeyTypeDrafter

    drafter = KeyTypeDrafter(index_dir=args.index_dir)
    drafts = drafter.draft(
        top_n=args.top_n,
        min_score=args.min_score,
        from_live=args.from_live,
        dsn=args.dsn,
        sample_limit=args.sample_limit,
    )
    if not drafts:
        print("患者キー列が索引にありません（ingest で is_patient_key 宣言を確認）")
        return 0
    for d in drafts:
        cand = ", ".join(f"{c.key_type}={c.score:.2f}" for c in d.candidates) or "(候補なし)"
        print(f"[{d.table}].{d.physical_name}: {cand}")
        if d.fingerprint:
            print(f"    指紋: {d.fingerprint}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            for d in drafts:
                fh.write(
                    _json.dumps(
                        {
                            "unit_id": d.unit_id,
                            "table": d.table,
                            "physical_name": d.physical_name,
                            "fingerprint": d.fingerprint,
                            "candidates": [
                                {"key_type": c.key_type, "score": round(c.score, 4)}
                                for c in d.candidates
                            ],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        print(f"候補を書き出しました（要人手確定）: {args.out}")
    print("  ※ 候補提示のみ。key_type の確定は人手（knowledge を編集して ingest）。")
    return 0


def cmd_draft_knowledge(args: argparse.Namespace) -> int:
    """information_schema＋定義書から知識層 YAML を LLM で下書きする（要 Ollama）。"""
    from pathlib import Path

    from kawasemi_indexer.connections import get_registry
    from kawasemi_indexer.ingestion.drafter import KnowledgeDrafter
    from kawasemi_indexer.ingestion.info_schema import reflect_snapshot
    from kawasemi_indexer.ingestion.knowledge import load_knowledge, save_knowledge

    resolved_dsn = args.dsn or get_registry().dsn_of(args.connection)
    if not resolved_dsn:
        print(
            f"エラー: 接続 {args.connection} の DSN を解決できません（--dsn か connections.yaml）"
        )
        return 1

    # --only-missing / --resume は既存 knowledge を土台にする（再開・追補）。
    existing = None
    if args.only_missing or args.resume:
        if not Path(args.out).exists():
            print(f"エラー: --only-missing/--resume には既存の {args.out} が必要です")
            return 1
        existing = load_knowledge(args.out)
        print(f"既存 knowledge を読み込み: {args.out}（補間{len(existing.enrichments)}件）")

    print(f"information_schema を reflection 中: connection={args.connection}")
    snapshot = reflect_snapshot(
        resolved_dsn, connection_id=args.connection, db_group=args.db_group, schema=args.schema
    )
    drafter = KnowledgeDrafter(model=args.model)
    knowledge = asyncio.run(
        drafter.draft(
            connection_id=args.connection,
            db_group=args.db_group,
            snapshot=snapshot,
            tabular_paths=args.doc,
            enrich_only_missing=args.only_missing,
            enrich_tables=args.enrich_tables or None,
            enrich_limit=args.enrich_limit,
            enrich_near=args.near or None,
            enrich_dry_run=args.dry_run_enrich,
            existing=existing,
        )
    )
    if args.dry_run_enrich:
        print(
            "dry-run: 補間対象の内訳のみ表示しました（YAML は書き出していません）。ログの『論理名補間 選択』を参照。"
        )
        return 0
    save_knowledge(knowledge, args.out)
    print(f"下書きを書き出しました（要人手確認）: {args.out}")
    print(f"確認・修正後: kawasemi-indexer ingest {args.out} --dsn <DSN>")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """索引を1クエリで点検する（検索パイプライン・top-k 表示）。"""
    from kawasemi_indexer.embedder import OllamaEmbedder
    from kawasemi_indexer.ref_links import RefLinkIndex
    from kawasemi_indexer.search_pipeline import run_search
    from kawasemi_indexer.store import get_store
    from kawasemi_indexer.synonyms import QueryRewriter

    store = get_store(args.index_dir)
    model = args.model or settings.embedding_model
    if not store.is_searchable(model):
        print(
            "エラー: 索引が検索不可です（未ロード or build_info のモデル/バージョン不一致）。\n"
            f"  期待モデル={model}。build_info.json と embedding_model を確認してください。"
        )
        return 1
    embedder = OllamaEmbedder(
        model=model, base_url=settings.embedding_host, timeout=settings.ollama_timeout
    )
    # 施策の積み上げ: 既定は全施策（ref）。--no-* で段階的に落とす（点検・比較用）。
    if args.no_hybrid:
        mode = "embedding"
    elif args.no_rewrite:
        mode = "hybrid"
    else:
        mode = "ref"
    outcome = run_search(
        store,
        embedder,
        args.query,
        mode=mode,
        top_k=args.top_k,
        min_score=args.min_score,
        rewriter=QueryRewriter.load(args.index_dir) if mode in ("rewrite", "ref") else None,
        ref_links=RefLinkIndex.load(args.index_dir) if mode == "ref" else None,
    )
    if outcome.rewrite is not None and outcome.rewrite.matched:
        srcs = ", ".join(f"{p['label']}" for p in outcome.rewrite.provenance[:3])
        print(
            f"↳ クエリ展開（{len(outcome.rewrite.expansions)}語 / 展開元: {srcs}）: "
            f"{outcome.rewrite.expansions}"
        )
    if outcome.ref_masters:
        desc = ", ".join(
            f"{m['connection_id']}/{m['db_qualified']}({m['via']})" for m in outcome.ref_masters
        )
        print(f"↳ 参照リンクチャネル発動: {desc} → 候補 {len(outcome.ref_candidates)} 列")
    if not outcome.hits:
        print("候補なし（足切り or 索引空）")
        return 0
    for h in outcome.hits:
        tag = f" [{h.match_type}]" if h.match_type else ""
        if h.lexical_score is not None:
            tag += f" lex={h.lexical_score:.2f}"
        if h.ref_status is not None:
            tag += f" ref:{h.ref_status}" + ("" if h.ref_status == "confirmed" else "⚑")
        print(f"[{h.cand_id}] score={h.score:.4f}{tag} {h.unit.kind}: {h.unit.label}")
        if h.unit.snippet:
            print(f"      {h.unit.snippet}")
        # FK逆引き: master hit なら、それを参照する患者表FK列を表示。
        ref_cols = outcome.ref_columns_by_master.get(h.unit.unit_id)
        if ref_cols:
            shown = ", ".join(f"{uid}({role}/{status})" for uid, role, status in ref_cols[:5])
            print(f"      ↳ 参照する患者表FK列: {shown}")
    return 0


def cmd_build_synonyms(args: argparse.Namespace) -> int:
    """master_index.jsonl から標準コード変換テーブル synonym_index.jsonl を生成する。"""
    from kawasemi_indexer.synonyms import build_synonym_index

    path = build_synonym_index(args.index_dir, out_dir=args.out)
    lines = sum(1 for _ in path.open("r", encoding="utf-8"))
    print(f"完了: {path} に {lines} エントリの変換テーブルを書き出しました")
    print("  search はこのテーブルを自動で使い、クエリを別名・標準コードへ展開します。")
    return 0


def cmd_draft_ref_links(args: argparse.Namespace) -> int:
    """列→参照先マスタのリンクを下書きする（ルール＋任意で LLM。要人手確認）。"""
    from kawasemi_indexer.ref_links import (
        _read_db_units,
        draft_llm_links,
        draft_rule_links,
        llm_candidates,
        merge_links,
        rules_for,
        write_ref_links,
    )
    from kawasemi_indexer.site_rules import load_indexer_rules

    units = _read_db_units(args.index_dir)
    if not units:
        print(f"エラー: {args.index_dir} に db_index.jsonl が見つかりません")
        return 1

    masters = load_indexer_rules().ref_link_masters
    if not masters:
        print(
            f"注意: {settings.indexer_rules_file} に ref_link_masters が無いので、"
            "参照リンクは下書きしません"
        )
    rule_links = draft_rule_links(units, rules_for(masters))
    print(f"ルールベース: {len(rule_links)} リンク")

    llm_links = []
    if not args.no_llm and masters:
        model = args.model or settings.ollama_model
        rule_ids = {ln.column_unit_id for ln in rule_links}
        candidates = llm_candidates(units, exclude_ids=rule_ids)
        print(f"LLM 判定候補: {len(candidates)} 列（model={model}）...")
        llm_links = asyncio.run(draft_llm_links(candidates, model=model, masters=masters))
        print(f"LLM 推論: {len(llm_links)} リンク")

    merged = merge_links(rule_links, llm_links)
    path = write_ref_links(merged, args.out or args.index_dir)
    print(f"完了: {path} に {len(merged)} リンクを書き出しました（status: rule/inferred）")
    print("  人手確認後、確定したリンクの status を confirmed に書き換えてください。")
    print(
        "  search / eval / debug / serve はこのテーブルを参照リンクチャネルとして自動で使います。"
    )
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """ゴールドセットで検索精度（recall@k / MRR）をモード別に測る（embedding 必須）。"""
    from kawasemi_indexer.embedder import OllamaEmbedder
    from kawasemi_indexer.eval import MODES, evaluate, format_report, load_gold
    from kawasemi_indexer.ref_links import RefLinkIndex
    from kawasemi_indexer.store import get_store
    from kawasemi_indexer.synonyms import QueryRewriter

    store = get_store(args.index_dir)
    model = args.model or settings.embedding_model
    if not store.is_searchable(model):
        print(
            "エラー: 索引が検索不可です（未ロード or build_info のモデル/バージョン不一致）。\n"
            f"  期待モデル={model}。build_info.json と embedding_model を確認してください。"
        )
        return 1

    gold = load_gold(args.gold)
    if not gold:
        print(f"ゴールドセットが空です: {args.gold}")
        return 1

    embedder = OllamaEmbedder(
        model=model, base_url=settings.embedding_host, timeout=settings.ollama_timeout
    )
    rewriter = QueryRewriter.load(args.index_dir)
    ref_links = RefLinkIndex.load(args.index_dir)
    modes = MODES if args.mode == "all" else (args.mode,)

    bi = store.build_info
    print(f"索引: {args.index_dir}")
    if bi:
        print(
            f"  model={bi.embedding_model} schema_version={bi.schema_version} "
            f"unit_count={bi.unit_count} built_at={bi.built_at}"
        )
    if "rewrite" in modes or "ref" in modes:
        print(f"  変換テーブル: {rewriter.entry_count}エントリ")
    if "ref" in modes:
        print(f"  参照リンク: {len(ref_links)}件")
    print()

    reports = [
        evaluate(store, embedder, gold, mode=m, rewriter=rewriter, ref_links=ref_links)
        for m in modes
    ]
    for line in format_report(reports):
        print(line)

    if args.show_misses:
        for r in reports:
            if r.misses:
                print(f"\n[{r.mode}] 取りこぼし {len(r.misses)}件:")
                for q in r.misses[:30]:
                    print(f"  - {q}")
    return 0


def cmd_debug(args: argparse.Namespace) -> int:
    """対話的な索引診断コンソールを起動する（embedding 必須）。"""
    import os

    from kawasemi_indexer.debug_repl import Session, run
    from kawasemi_indexer.embedder import OllamaEmbedder
    from kawasemi_indexer.ref_links import RefLinkIndex
    from kawasemi_indexer.store import get_store
    from kawasemi_indexer.synonyms import QueryRewriter

    store = get_store(args.index_dir)
    model = args.model or settings.embedding_model
    if not store.is_searchable(model):
        print(
            "エラー: 索引が検索不可です（未ロード or build_info のモデル/バージョン不一致）。\n"
            f"  期待モデル={model}。build_info.json と embedding_model を確認してください。\n"
            "  debug は embedding 必須です（docs/kawasemi_indexer_debug_spec.md §4）。"
        )
        return 1
    embedder = OllamaEmbedder(
        model=model, base_url=settings.embedding_host, timeout=settings.ollama_timeout
    )
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    session = Session(
        store=store,
        embedder=embedder,
        model=model,
        top_k=args.top_k,
        min_score=args.min_score,
        color=color,
        rewriter=QueryRewriter.load(args.index_dir),
        ref_links=RefLinkIndex.load(args.index_dir),
    )
    return run(session)


def _serve_token(token_file: str | None) -> str:
    """serve の Bearer トークンを決める。優先順位: --token-file → 設定（INDEXER_SERVE_TOKEN）→ 空。

    ファイルは本人しか読めないこと（0600 相当）を要し、そうでなければ拒否する。
    空を返したときは、呼び出し側が起動のたびに生成する。
    """
    if token_file:
        path = Path(token_file)
        if not path.is_file():
            raise ValueError(f"--token-file が読めません: {token_file}")
        if path.stat().st_mode & 0o077:
            raise ValueError(
                f"--token-file は本人以外が読める権限です（chmod 600 してください）: {token_file}"
            )
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 16:
            raise ValueError("--token-file のトークンが短すぎます（16文字以上）")
        return token
    token = settings.indexer_serve_token.strip()
    if token and len(token) < 16:
        raise ValueError("INDEXER_SERVE_TOKEN が短すぎます（16文字以上）")
    return token


_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", ""})


def _browser_url(host: str, port: int) -> str:
    """起動時に表示する、ブラウザで開ける URL。

    `0.0.0.0` / `::` は「全インターフェースで待ち受ける」指定で、宛先としては使えない
    （Host 検査も通らない）。同じマシンから開ける `localhost` を表示する。IPv6 リテラルは
    URL では角括弧で囲む。
    """
    shown = "localhost" if host in _WILDCARD_HOSTS else host
    if ":" in shown and not shown.startswith("["):
        shown = f"[{shown}]"
    return f"http://{shown}:{port}/"


def cmd_serve(args: argparse.Namespace) -> int:
    """ローカル索引診断 WebUI/JSON API を起動する（embedding 必須）。"""
    from kawasemi_indexer.store import get_store

    store = get_store(args.index_dir)
    model = args.model or settings.embedding_model
    if not store.is_searchable(model):
        print(
            "エラー: 索引が検索不可です（未ロード or build_info のモデル/バージョン不一致）。\n"
            f"  期待モデル={model}。build_info.json と embedding_model を確認してください。\n"
            "  serve は embedding 必須です（docs/kawasemi_indexer_debug_spec.md §4）。"
        )
        return 1

    try:
        import uvicorn

        from kawasemi_indexer.webserver import create_app, is_loopback_bind
    except ImportError:
        print(
            "エラー: WebUI 依存が未導入です。次でインストールしてください:\n  uv sync --extra webui"
        )
        return 1

    loopback = is_loopback_bind(args.host)
    if args.no_auth and not loopback:
        print(
            "エラー: --no-auth はループバック（127.0.0.1 / ::1 / localhost）への bind でだけ使えます。"
        )
        return 1
    token: str | None = None
    token_given = False
    if not args.no_auth:
        try:
            token = _serve_token(args.token_file)
        except ValueError as e:
            print(f"エラー: {e}")
            return 1
        token_given = bool(token)
        token = token or secrets.token_urlsafe(32)
    if not loopback:
        print(
            f"警告: {args.host} はループバックではありません。ネットワークから届きます。"
            " Host は localhost 系と --allowed-host で指定した名前だけを通し、"
            "/api/* は Bearer トークンを要します（TLS は無いので、信頼できる経路でだけ使ってください）。"
        )
    app = create_app(
        index_dir=args.index_dir,
        model=model,
        token=token,
        insecure_no_auth=args.no_auth,
        allowed_hosts=[*args.allowed_host, args.host],
    )
    url = _browser_url(args.host, args.port)
    print(f"索引診断 WebUI を起動: {url} （Ctrl-C で停止）")
    if args.no_auth:
        print("  認証なし（--no-auth）。ループバックの bind でのみ有効です。")
    elif token_given:
        print("  /api/* は Bearer トークンが必要です（指定されたトークン）。")
        print(f"  ブラウザは {url}#token=<トークン> を開く。")
    else:
        print(f"  ブラウザはこの URL を開く（トークンは今回だけ有効）: {url}#token={token}")
    if args.host in _WILDCARD_HOSTS:
        print(
            f"  {args.host} は全インターフェースで待ち受ける指定で、ブラウザで開く宛先ではありません。"
            "上の URL は同じマシンから開く用です。"
            "別のマシンから開くときは、その名前を --allowed-host に指定して、"
            "その名前の URL を使ってください（Host 検査は緩めていません）。"
        )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kawasemi-indexer", description="KAWASEMI リソース索引のビルド/取り込みツール"
    )
    parser.add_argument("--version", action="version", version=f"kawasemi-indexer {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="確認済知識層 YAML → column_index.jsonl 一式を生成")
    p_ingest.add_argument("knowledge", help="確認済知識層 YAML のパス")
    p_ingest.add_argument("--out", default=settings.resource_index_dir, help="出力ディレクトリ")
    p_ingest.add_argument("--dsn", default=None, help="DSN明示指定（無指定で connections.yaml）")
    p_ingest.add_argument("--schema", default=None, help="スキーマ名")
    p_ingest.add_argument(
        "--fix",
        action="store_true",
        help="定義書の物理識別子を NFKC 正規化して取り込む（全角→半角等）",
    )
    p_ingest.add_argument(
        "--force", action="store_true", help="検品で error があっても取り込みを続行する"
    )
    p_ingest.add_argument(
        "--catalog",
        default=None,
        help="実行先DBの表一覧（schema_name|table_name|行数）。physical_schemas が複数で、"
        "reflection したスキーマが候補に無いときに使う（無ければ DSN から列挙）",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_lint = sub.add_parser("lint", help="定義書（Excel/CSV）を検品して qa レポートを出力")
    p_lint.add_argument("knowledge", help="確認済知識層 YAML のパス（tabular_sources を検品）")
    p_lint.add_argument(
        "--out", default=settings.resource_index_dir, help="qa レポート出力先ディレクトリ"
    )
    p_lint.add_argument(
        "--fix",
        action="store_true",
        help="安全な正規化（NFKC）を適用した状態で検品し、適用内容を記録する",
    )
    p_lint.set_defaults(func=cmd_lint)

    p_pdf = sub.add_parser(
        "extract-pdf-doc", help="紙・スキャンPDF定義書からカラム定義を復元し正準CSVを出力"
    )
    p_pdf.add_argument("pdf", help="PDF 定義書のパス")
    p_pdf.add_argument("--out", required=True, help="出力する正準 CSV のパス")
    p_pdf.add_argument("--model", default=None, help="LLM モデル名（無指定で設定値）")
    p_pdf.add_argument(
        "--vision-model", default=None, help="スキャンOCR用 vision モデル（無指定で --model）"
    )
    p_pdf.add_argument(
        "--no-ocr", action="store_true", help="スキャンページの画像OCRを行わない（テキスト層のみ）"
    )
    p_pdf.set_defaults(func=cmd_extract_pdf_doc)

    p_keytypes = sub.add_parser(
        "draft-key-types", help="患者キー列に key_type 候補を提案（--from-live で指紋補強）"
    )
    p_keytypes.add_argument("--index-dir", default=settings.resource_index_dir)
    p_keytypes.add_argument("--top-n", type=int, default=3, help="列ごとの候補数")
    p_keytypes.add_argument("--min-score", type=float, default=0.0)
    p_keytypes.add_argument("--out", default=None, help="候補を JSONL で書き出す先")
    p_keytypes.add_argument(
        "--from-live",
        action="store_true",
        help="読み取り専用接続で値をサンプリングしフォーマット指紋で候補を補強（生値は非保持）",
    )
    p_keytypes.add_argument(
        "--dsn", default=None, help="--from-live の DSN（無指定で connections.yaml）"
    )
    p_keytypes.add_argument(
        "--sample-limit", type=int, default=None, help="1列あたりのサンプリング上限行数"
    )
    p_keytypes.set_defaults(func=cmd_draft_key_types)

    p_draft = sub.add_parser("draft-knowledge", help="知識層 YAML を LLM で下書き")
    p_draft.add_argument("--connection", required=True, help="connection_id（connections.yaml）")
    p_draft.add_argument("--db-group", required=True, help="db_group")
    p_draft.add_argument("--out", required=True, help="出力する knowledge.yaml のパス")
    p_draft.add_argument("--doc", action="append", default=[], help="定義書ファイル（複数可）")
    p_draft.add_argument("--dsn", default=None)
    p_draft.add_argument("--schema", default=None)
    p_draft.add_argument("--model", default=None, help="LLM モデル名（無指定で設定値）")
    # 論理名補間の絞り込み（大規模スキーマで全列一括の LLM コストを避ける）。
    p_draft.add_argument(
        "--only-missing",
        action="store_true",
        help="--out の既存 knowledge を土台に、未補間の列だけを追補（冪等・再開。--resume と同義）",
    )
    p_draft.add_argument(
        "--resume",
        action="store_true",
        help="--out の既存 knowledge の構造下書き（役割/患者キー等）を再利用し、未補間の列だけ追補",
    )
    p_draft.add_argument(
        "--enrich-tables",
        action="append",
        default=[],
        metavar="GLOB",
        help="補間対象を表名グロブで限定（例 'DEMO.*'。複数可）",
    )
    p_draft.add_argument(
        "--enrich-limit", type=int, default=None, help="補間する列数の上限（優先度上位から）"
    )
    p_draft.add_argument(
        "--near",
        action="append",
        default=[],
        metavar="TERM",
        help="この語に物理名・表名が一致する列を優先（拾えなかった項目起点。複数可）",
    )
    p_draft.add_argument(
        "--dry-run-enrich",
        action="store_true",
        help="補間の対象と内訳のみ表示し補間 LLM を呼ばない（コスト見積り。"
        "新規スキーマでは分類・患者キー下書きの LLM は動くため、純粋な見積りは --resume 併用）",
    )
    p_draft.set_defaults(func=cmd_draft_knowledge)

    p_build = sub.add_parser("build", help="column_index.jsonl → embedding 索引を生成")
    p_build.add_argument("--db-source", default=None, help="DB定義書ディレクトリ（既定で設定値）")
    p_build.add_argument("--fhir-source", default=None)
    p_build.add_argument("--file-source", default=None)
    p_build.add_argument("--file-manifest", default=None)
    p_build.add_argument(
        "--out", default=settings.resource_index_dir, help="索引の出力ディレクトリ"
    )
    p_build.add_argument("--model", default=settings.embedding_model, help="embedding モデル名")
    p_build.set_defaults(func=cmd_build)

    p_enrich = sub.add_parser(
        "enrich-physical",
        help="既存の索引の db 単位へ実行用の識別子（payload.physical）を追記",
    )
    p_enrich.add_argument("--base", required=True, help="既存の索引ディレクトリ（変更しない）")
    p_enrich.add_argument("--out", required=True, help="書き出し先（base と別）")
    p_enrich.add_argument(
        "--knowledge",
        required=True,
        nargs="+",
        help="知識層 YAML（db_group → physical_schemas / doc_header を読む。接続ごとに1つ）",
    )
    p_enrich.add_argument(
        "--catalog",
        required=True,
        help="実行先DBの表一覧（schema_name|table_name|行数 の1行1表）",
    )
    p_enrich.set_defaults(func=cmd_enrich_physical)

    p_search = sub.add_parser("search", help="索引の点検（NL → 候補ランキング）")
    p_search.add_argument("query", help="検索クエリ")
    p_search.add_argument("--index-dir", default=settings.resource_index_dir)
    p_search.add_argument("--top-k", type=int, default=8)
    p_search.add_argument("--min-score", type=float, default=0.0)
    p_search.add_argument("--model", default=None, help="embedding モデル名（無指定で設定値）")
    p_search.add_argument(
        "--no-hybrid", action="store_true", help="字面チャネルを使わず純 embedding 検索にする"
    )
    p_search.add_argument(
        "--no-rewrite", action="store_true", help="変換テーブルによるクエリ展開を無効にする"
    )
    p_search.set_defaults(func=cmd_search)

    p_syn = sub.add_parser(
        "build-synonyms", help="master_index.jsonl → 標準コード変換テーブル（クエリ展開用）"
    )
    p_syn.add_argument("--index-dir", default=settings.resource_index_dir)
    p_syn.add_argument("--out", default=None, help="出力先（無指定で index-dir に併置）")
    p_syn.set_defaults(func=cmd_build_synonyms)

    p_ref = sub.add_parser(
        "draft-ref-links", help="列→参照先マスタのリンクを下書き（ルール＋LLM。要人手確認）"
    )
    p_ref.add_argument("--index-dir", default=settings.resource_index_dir)
    p_ref.add_argument("--out", default=None, help="出力先（無指定で index-dir に併置）")
    p_ref.add_argument("--no-llm", action="store_true", help="ルールベースのみ（Ollama 不要）")
    p_ref.add_argument("--model", default=None, help="LLM モデル名（無指定で設定値）")
    p_ref.set_defaults(func=cmd_draft_ref_links)

    p_eval = sub.add_parser("eval", help="ゴールドセットで検索精度を測る（recall@k / MRR）")
    p_eval.add_argument("--gold", required=True, help="ゴールドセット jsonl（query/unit_id/tags）")
    p_eval.add_argument("--index-dir", default=settings.resource_index_dir)
    p_eval.add_argument(
        "--mode",
        default="all",
        choices=["all", "embedding", "hybrid", "rewrite", "ref"],
        help="評価する検索モード（既定 all で 4 モード比較）",
    )
    p_eval.add_argument("--model", default=None, help="embedding モデル名（無指定で設定値）")
    p_eval.add_argument("--show-misses", action="store_true", help="取りこぼしクエリを列挙")
    p_eval.set_defaults(func=cmd_eval)

    p_debug = sub.add_parser("debug", help="対話的な索引診断コンソール（embedding 必須）")
    p_debug.add_argument("--index-dir", default=settings.resource_index_dir)
    p_debug.add_argument("--top-k", type=int, default=8)
    p_debug.add_argument("--min-score", type=float, default=0.0)
    p_debug.add_argument("--model", default=None, help="embedding モデル名（無指定で設定値）")
    p_debug.set_defaults(func=cmd_debug)

    p_serve = sub.add_parser(
        "serve", help="ローカル索引診断 WebUI/JSON API を起動（embedding 必須）"
    )
    p_serve.add_argument("--index-dir", default=settings.resource_index_dir)
    p_serve.add_argument("--host", default="127.0.0.1", help="バインドアドレス（既定ローカル限定）")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Host ヘッダとして足して許す名前（繰り返し可）。localhost 系と bind アドレスは常に許す",
    )
    p_serve.add_argument(
        "--token-file",
        default=None,
        help="Bearer トークンを書いたファイル（0600）。無指定なら INDEXER_SERVE_TOKEN、"
        "それも無ければ起動のたびに生成して表示する",
    )
    p_serve.add_argument(
        "--no-auth",
        action="store_true",
        help="トークン認証を外す（ループバック bind のときだけ許す。ローカルの開発用）",
    )
    p_serve.add_argument("--model", default=None, help="embedding モデル名（無指定で設定値）")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
