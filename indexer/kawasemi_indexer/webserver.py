"""索引診断 WebUI / JSON API（ローカル専用・FastAPI）

`kawasemi-indexer serve` から起動される。CLI デバッガ（debug_repl）と同じ診断ロジックを
webapi.py 経由で呼び、構造化 JSON と単一ページ UI を提供する。

設計:
- ローカル専用。host=127.0.0.1 既定。同一オリジンで CORS は出さない。
- 認証と Host/Origin の検査を必須にする（DNS rebinding と、ループバック以外への bind への備え）:
  Host ヘッダは許可一覧（localhost / 127.0.0.1 / [::1] ＋ 明示した名前）だけ、Origin /
  Sec-Fetch-Site は同一オリジンだけを通し、`/api/*` は Bearer トークンを要する。
  トークン無しで起動できるのは、ループバックに限って明示した場合（`--no-auth`）だけ。
- 索引（store）と embedder はアプリ生成時に 1 回だけ構築し全リクエストで共有（遅延ロード済）。
- フィルタ/top_k/min_score はステートレス（リクエスト毎に webapi 側で短命 Session を組む）。
- embedding 必須。is_searchable=False なら起動を拒否する（誤検索防止, debug_spec §4）。
"""

from __future__ import annotations

import hmac
import ipaddress
import re
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

from kawasemi_indexer.config import settings
from kawasemi_indexer.logging_config import get_logger
from kawasemi_indexer.store import get_store
from kawasemi_indexer.webapi import (
    GrepRequest,
    InvalidKind,
    InvalidMode,
    SearchRequest,
    SearchResponse,
    UnitNotFound,
    WhyRequest,
    build_grep_response,
    build_search_response,
    build_show_response,
    build_status_response,
    build_why_response,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from kawasemi_indexer.embedder import Embedder

logger = get_logger(__name__)

T = TypeVar("T")

_OLLAMA_ERROR = "Ollama に接続できません。`ollama serve` が起動しているか確認してください。"

# Host ヘッダとして常に許す名前（ポートは見ない）。これ以外は --allowed-host で明示する。
LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})


def is_loopback_bind(host: str) -> bool:
    """bind アドレスがループバックか。名前は localhost だけ認め、それ以外は IP で判定する。"""
    h = host.strip().lower().strip("[]")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


_HOST_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")
_HOST_IPV6_RE = re.compile(r"^\[[0-9a-f:.]+\]$")
_HOST_PORT_RE = re.compile(r"^[0-9]{1,5}$")


def _host_name(header: str) -> str:
    """Host ヘッダからポートを除いた名前を返す（`[::1]:8000` → `[::1]`）。

    形が正しくない値は空文字列（どの許可リストにも入らない）にする。`host` か `host:port`
    だけを認め、ポートは数字だけ。`/` `?` `#` `@`・空白・制御文字・ASCII 以外を含む値は
    すべて拒否する。Host ヘッダは URL の組み立てにも使われるので（`localhost:8000?` が
    パスを空にした、再スキャン F3）、検査の前にここで落とす。
    """
    h = header.strip().lower()
    if not h or not h.isascii() or not h.isprintable() or any(c in h for c in " /?#@\\"):
        return ""
    if h.startswith("["):
        end = h.find("]")
        if end == -1:
            return ""
        name, rest = h[: end + 1], h[end + 1 :]
        if not _HOST_IPV6_RE.match(name):
            return ""
    else:
        name, sep, port = h.partition(":")
        rest = sep + port
        if not _HOST_LABEL_RE.match(name):
            return ""
    if rest:
        if not rest.startswith(":") or not _HOST_PORT_RE.match(rest[1:]) or int(rest[1:]) > 65535:
            return ""
    return name


# トークン無しで届くパス。診断 UI の HTML だけ（API はすべてトークンを要する）。
_PUBLIC_PATHS = frozenset({"/"})


def _route_path(scope: Mapping[str, Any]) -> str:
    """ルーティングに使われるパス（root_path の接頭辞を除く）。Host ヘッダには依存しない。"""
    path = str(scope.get("path", ""))
    root = str(scope.get("root_path", "")).rstrip("/")
    if root and path.startswith(root):
        path = path[len(root) :]
    return path


def allowed_host_names(bind_host: str | None, extra: Iterable[str] = ()) -> frozenset[str]:
    """許す Host 名の集合。bind アドレスが名前や個別の IP のときはそれも許す（0.0.0.0 などは除く）。"""
    names = set(LOOPBACK_HOST_NAMES)
    for h in (*extra, *([bind_host] if bind_host else [])):
        name = h.strip().lower()
        if not name:
            continue
        try:
            ip = ipaddress.ip_address(name.strip("[]"))
        except ValueError:
            names.add(name)
            continue
        if not ip.is_unspecified:
            names.add(f"[{ip}]" if ip.version == 6 else str(ip))
    return frozenset(names)


def create_app(
    index_dir: str | None = None,
    model: str | None = None,
    embedder: Embedder | None = None,
    *,
    token: str | None = None,
    insecure_no_auth: bool = False,
    allowed_hosts: Iterable[str] = (),
) -> FastAPI:
    """索引診断 FastAPI アプリを構築する。

    embedder を渡すとそれを共有利用（テストで HashingEmbedder を注入可能）。
    省略時は設定値で OllamaEmbedder を生成する。

    `/api/*` は `Authorization: Bearer <token>` を要する。`token` が空で `insecure_no_auth` も
    無いときは起動を拒否する（認証無しは明示したときだけ）。`allowed_hosts` は loopback の名前に
    足して許す Host 名（bind アドレスが名前や個別の IP のときは呼び出し側がそれも渡す）。
    """
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    if not token and not insecure_no_auth:
        raise ValueError(
            "token が必要です（認証無しで起動するなら insecure_no_auth=True を明示する）"
        )
    hosts = allowed_host_names(None, allowed_hosts)
    expected = token.encode() if token else None

    store = get_store(index_dir)
    resolved_model = model or settings.embedding_model
    if not store.is_searchable(resolved_model):
        raise RuntimeError(
            "索引が検索不可です（未ロード or build_info のモデル/バージョン不一致）。"
            f" 期待モデル={resolved_model}。build_info.json と embedding_model を確認してください。"
        )

    if embedder is None:
        from kawasemi_indexer.embedder import OllamaEmbedder

        embedder = OllamaEmbedder(
            model=resolved_model,
            base_url=settings.embedding_host,
            timeout=settings.ollama_timeout,
        )

    # unit_id -> unit の O(1) 索引（:show 用）。起動時に 1 回だけ構築。
    unit_index = {u.unit_id: u for u in store.units}

    # 検索パイプラインの成果物（変換テーブル・参照リンク）。未配置なら各施策は不発で縮退。
    from kawasemi_indexer.ref_links import RefLinkIndex
    from kawasemi_indexer.synonyms import QueryRewriter

    resolved_dir = index_dir or settings.resource_index_dir
    rewriter = QueryRewriter.load(resolved_dir)
    ref_links = RefLinkIndex.load(resolved_dir)

    # /docs と /openapi.json は出さない（診断 UI は / だけで足りる）。
    app = FastAPI(
        title="kawasemi-indexer 索引診断", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.middleware("http")
    async def _guard(request: Request, call_next: Callable[[Request], Any]) -> Any:
        """Host 許可一覧 → 同一オリジン → Bearer トークン、の順に検査する。"""
        host = request.headers.get("host", "")
        if _host_name(host) not in hosts:
            return JSONResponse({"detail": "許可されていない Host です"}, status_code=400)
        origin = request.headers.get("origin")
        if origin is not None and urlsplit(origin).netloc.lower() != host.strip().lower():
            return JSONResponse({"detail": "別オリジンからの要求は受け付けません"}, status_code=403)
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return JSONResponse({"detail": "別オリジンからの要求は受け付けません"}, status_code=403)
        # 認証の要否は、Host から組み立てられる request.url ではなく、ルーティングに使われる
        # scope のパスで決める。公開するのは `/` だけで、それ以外は（存在しないパスも）
        # トークンを要する（失敗時に閉じる）。
        if expected is not None and _route_path(request.scope) not in _PUBLIC_PATHS:
            auth = request.headers.get("authorization", "")
            scheme, _, given = auth.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                given.strip().encode(), expected
            ):
                return JSONResponse(
                    {"detail": "トークンが必要です"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    def _embed_guard(fn: Callable[[], T]) -> T:
        """embedding 呼び出し中の例外を 503（Ollama 接続不可）へ変換する。"""
        try:
            return fn()
        except (InvalidKind, InvalidMode, HTTPException):
            raise
        except Exception as e:  # noqa: BLE001 — Ollama/httpx 由来の多様な例外を一括で 503 化
            logger.warning(f"検索中のエラー: {e!r}")
            raise HTTPException(status_code=503, detail=_OLLAMA_ERROR) from e

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        return build_status_response(store, resolved_model)

    @app.post("/api/search", response_model=SearchResponse)
    def api_search(req: SearchRequest) -> SearchResponse:
        try:
            return _embed_guard(
                lambda: build_search_response(
                    store, embedder, resolved_model, req, rewriter, ref_links
                )
            )
        except (InvalidKind, InvalidMode) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.get("/api/show/{unit_id:path}")
    def api_show(unit_id: str) -> dict[str, Any]:
        try:
            return build_show_response(unit_index, unit_id)
        except UnitNotFound as e:
            raise HTTPException(
                status_code=404, detail=f"unit_id が見つかりません: {unit_id}"
            ) from e

    @app.post("/api/grep")
    def api_grep(req: GrepRequest) -> dict[str, Any]:
        try:
            return build_grep_response(store, req)
        except InvalidKind as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.post("/api/why")
    def api_why(req: WhyRequest) -> dict[str, Any]:
        try:
            return _embed_guard(
                lambda: build_why_response(
                    store, embedder, resolved_model, req, rewriter, ref_links
                )
            )
        except (InvalidKind, InvalidMode) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML

    return app


# --------------------------------------------------------------------------- #
# 単一ページ UI（外部 CDN 非依存・インライン CSS/JS）
# --------------------------------------------------------------------------- #
_INDEX_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kawasemi-indexer 索引診断</title>
<script>
/* テーマ確定（描画前・FOUC 回避）。本体と同じ localStorage キーを使う。 */
(function(){try{var t=localStorage.getItem('kawasemi-theme');if(!t){t=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}if(t==='dark')document.documentElement.setAttribute('data-theme','dark');}catch(e){}})();
</script>
<style>
  /* Hallmark · redesign · diagnostic console · genre: modern-minimal · tone: technical
   * theme: KAWASEMI design-system（本体 tokens.css をインライン）· light/dark 2系統
   * accent: cobalt oklch(54% .17 256) · system-ui + JetBrains Mono(mono) · 生値禁止・トークン参照のみ
   */

  /* ── KAWASEMI トークン正本（本体 kawasemi/web/static/css/tokens.css と同値） ── */
  :root {
    --color-paper:      oklch(98.5% 0.004 250);
    --color-surface:    oklch(100%  0     0);
    --color-surface-2:  oklch(97%   0.005 250);

    --color-ink:        oklch(24% 0.02  262);
    --color-ink-2:      oklch(46% 0.018 262);
    --color-ink-3:      oklch(62% 0.014 262);

    --color-border:     oklch(91% 0.008 262);
    --color-border-2:   oklch(83% 0.01  262);

    --color-primary:        oklch(54% 0.17 256);
    --color-primary-strong: oklch(47% 0.18 256);
    --color-primary-tint:   oklch(96% 0.03 256);
    --color-primary-fg:     oklch(42% 0.15 256);

    --color-ai:        oklch(56% 0.17 296);
    --color-ai-strong: oklch(49% 0.18 296);
    --color-ai-tint:   oklch(96.5% 0.03 296);
    --color-ai-fg:     oklch(46% 0.16 296);

    --color-ok:        oklch(56% 0.13 150);
    --color-ok-strong: oklch(48% 0.13 150);
    --color-ok-tint:   oklch(95.5% 0.04 150);
    --color-ok-fg:     oklch(42% 0.12 150);

    --color-warn:        oklch(64% 0.13 70);
    --color-warn-strong: oklch(56% 0.13 65);
    --color-warn-tint:   oklch(95.5% 0.055 80);
    --color-warn-fg:     oklch(45% 0.11 65);

    --color-danger:        oklch(56% 0.19 25);
    --color-danger-strong: oklch(49% 0.20 25);
    --color-danger-tint:   oklch(95.5% 0.04 25);
    --color-danger-fg:     oklch(45% 0.16 25);

    --color-muted:      oklch(70% 0.012 262);
    --color-muted-tint: oklch(96% 0.004 262);
    --color-muted-fg:   oklch(40% 0.012 262);

    --color-on-accent: oklch(100% 0 0);

    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Kaku Gothic ProN",
                 "Yu Gothic", "Noto Sans JP", Meiryo, sans-serif;
    --font-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;

    --text-h2:   1.35rem;
    --text-h3:   1.05rem;
    --text-body: 0.95rem;
    --text-sm:   0.85rem;
    --text-xs:   0.78rem;
    --lh-body:    1.7;
    --lh-heading: 1.45;

    --space-xs:  0.25rem;
    --space-sm:  0.5rem;
    --space-md:  1rem;
    --space-lg:  1.5rem;
    --space-xl:  2rem;
    --space-2xl: 3rem;

    --radius-sm: 3px;
    --radius-md: 6px;

    --shadow-1: 0 1px 2px oklch(24% 0.02 262 / .06);
    --shadow-2: 0 4px 16px oklch(24% 0.02 262 / .10);

    --ease-out:    cubic-bezier(.2, 0, 0, 1);
    --ease-in-out: cubic-bezier(.4, 0, .2, 1);
    --dur-fast: 120ms;
    --dur:      160ms;

    --content-max: 1040px;  /* 診断リストの可読幅（本体 container-max より絞る） */
  }

  [data-theme="dark"] {
    --color-paper:      oklch(20%   0.012 262);
    --color-surface:    oklch(24%   0.013 262);
    --color-surface-2:  oklch(27.5% 0.013 262);

    --color-ink:        oklch(93% 0.008 262);
    --color-ink-2:      oklch(74% 0.011 262);
    --color-ink-3:      oklch(58% 0.011 262);

    --color-border:     oklch(33% 0.012 262);
    --color-border-2:   oklch(45% 0.013 262);

    --color-primary:        oklch(70% 0.15  256);
    --color-primary-strong: oklch(79% 0.13  256);
    --color-primary-tint:   oklch(31% 0.055 262);
    --color-primary-fg:     oklch(82% 0.12  256);

    --color-ai:        oklch(73% 0.15 296);
    --color-ai-strong: oklch(81% 0.13 296);
    --color-ai-tint:   oklch(31% 0.06 296);
    --color-ai-fg:     oklch(83% 0.12 296);

    --color-ok:        oklch(72% 0.14 150);
    --color-ok-strong: oklch(80% 0.13 150);
    --color-ok-tint:   oklch(30% 0.05 150);
    --color-ok-fg:     oklch(84% 0.13 150);

    --color-warn:        oklch(80% 0.13 78);
    --color-warn-strong: oklch(86% 0.11 80);
    --color-warn-tint:   oklch(32% 0.055 70);
    --color-warn-fg:     oklch(86% 0.12 80);

    --color-danger:        oklch(70% 0.17 25);
    --color-danger-strong: oklch(78% 0.15 25);
    --color-danger-tint:   oklch(32% 0.075 25);
    --color-danger-fg:     oklch(82% 0.13 25);

    --color-muted:      oklch(50% 0.012 262);
    --color-muted-tint: oklch(29% 0.008 262);
    --color-muted-fg:   oklch(78% 0.012 262);

    --color-on-accent: oklch(17% 0.02 262);

    --shadow-1: 0 1px 2px oklch(0% 0 0 / .45);
    --shadow-2: 0 4px 16px oklch(0% 0 0 / .55);
  }

  @media (prefers-reduced-motion: reduce) {
    :root { --dur-fast: 0.01ms; --dur: 0.01ms; }
  }

  /* ── Base ── */
  * { box-sizing: border-box; }
  html, body { overflow-x: clip; }
  body { margin:0; background:var(--color-paper); color:var(--color-ink);
         font-family:var(--font-sans); font-size:var(--text-body); line-height:var(--lh-body);
         -webkit-font-smoothing:antialiased; }

  /* ── Header（light topbar・hairline・sticky） ── */
  header { background:var(--color-surface); border-bottom:1px solid var(--color-border);
           padding:var(--space-sm) var(--space-lg); position:sticky; top:0; z-index:50; }
  .bar { display:flex; align-items:center; justify-content:space-between; gap:var(--space-md); }
  header h1 { margin:0; font-size:var(--text-h3); font-weight:600; line-height:var(--lh-heading);
              letter-spacing:-0.01em; min-width:0; overflow-wrap:anywhere; }
  .brand-dot { color:var(--color-primary); }
  #status { color:var(--color-ink-2); font-size:var(--text-sm); margin-top:var(--space-xs); }
  #status b { color:var(--color-ink); font-weight:600; font-family:var(--font-mono); font-variant-numeric:tabular-nums; }

  .theme-toggle { flex:none; cursor:pointer; width:34px; height:34px; padding:0;
                  display:grid; place-items:center; font-size:var(--text-body); line-height:1;
                  background:var(--color-surface); color:var(--color-ink-2);
                  border:1px solid var(--color-border-2); border-radius:999px;
                  transition:background var(--dur) var(--ease-out), color var(--dur) var(--ease-out); }
  .theme-toggle:hover { background:var(--color-surface-2); color:var(--color-ink); }
  .theme-toggle:focus-visible { outline:none;
                  box-shadow:0 0 0 3px color-mix(in oklch, var(--color-primary) 22%, transparent); }

  main { max-width:var(--content-max); margin:0 auto; padding:var(--space-lg); }

  /* ── Controls / form ── */
  .controls { display:flex; flex-wrap:wrap; gap:var(--space-sm); align-items:center; margin-bottom:var(--space-md); }
  input, select { background:var(--color-surface); color:var(--color-ink); font-family:inherit;
          border:1px solid var(--color-border-2); border-radius:var(--radius-sm);
          padding:var(--space-sm) 10px; font-size:var(--text-body); }
  input:focus, select:focus { outline:none; border-color:var(--color-primary);
          box-shadow:0 0 0 3px color-mix(in oklch, var(--color-primary) 22%, transparent); }
  #q { flex:1 1 280px; min-width:200px; }
  input[type=number] { width:84px; font-family:var(--font-mono); font-variant-numeric:tabular-nums; }
  label.lbl { color:var(--color-ink-2); font-size:var(--text-sm); display:flex; gap:var(--space-xs); align-items:center; }

  /* ── Buttons（本体 .btn--* 準拠） ── */
  button { cursor:pointer; font-family:inherit; font-size:var(--text-body); font-weight:500;
           border:1px solid transparent; border-radius:var(--radius-sm);
           padding:var(--space-sm) var(--space-md);
           transition:background var(--dur) var(--ease-out), border-color var(--dur) var(--ease-out), transform var(--dur-fast) var(--ease-out); }
  button:focus-visible { outline:none; box-shadow:0 0 0 3px color-mix(in oklch, var(--color-primary) 22%, transparent); }
  button:active { transform:translateY(1px); }
  .btn--primary { background:var(--color-primary); color:var(--color-on-accent); border-color:var(--color-primary); font-weight:600; }
  .btn--primary:hover { background:var(--color-primary-strong); border-color:var(--color-primary-strong); }
  .btn--secondary { background:var(--color-surface); color:var(--color-ink-2); border-color:var(--color-border-2); }
  .btn--secondary:hover { background:var(--color-surface-2); color:var(--color-ink); }

  .meta-line { color:var(--color-ink-2); font-size:var(--text-sm); margin:var(--space-sm) 0 var(--space-md); }

  /* ── Result cards ── */
  .card { background:var(--color-surface); border:1px solid var(--color-border); border-radius:var(--radius-md);
          padding:var(--space-md); margin-bottom:var(--space-sm); cursor:pointer; box-shadow:var(--shadow-1);
          transition:border-color var(--dur) var(--ease-out), box-shadow var(--dur) var(--ease-out); }
  .card:hover { border-color:var(--color-primary); box-shadow:var(--shadow-2); }
  .card .head { display:flex; gap:var(--space-sm); align-items:baseline; flex-wrap:wrap; }
  .rank { color:var(--color-ink-3); font-family:var(--font-mono); font-variant-numeric:tabular-nums; }
  .score { color:var(--color-primary-fg); font-family:var(--font-mono); font-variant-numeric:tabular-nums; font-weight:600; }
  .kind { font-size:var(--text-xs); padding:1px 7px; border-radius:var(--radius-sm);
          background:var(--color-surface-2); color:var(--color-ink-2); font-family:var(--font-mono); }
  .loc { font-weight:600; color:var(--color-ink); }
  .label { color:var(--color-ink-2); }

  /* ── Badges（状態色トークンへマッピング） ── */
  .badges { margin-top:var(--space-sm); display:flex; flex-wrap:wrap; gap:var(--space-xs); }
  .badge { font-size:var(--text-xs); padding:1px 8px; border-radius:999px; font-family:var(--font-mono);
           background:var(--color-muted-tint); color:var(--color-muted-fg); }
  .badge.vd    { background:var(--color-primary-tint); color:var(--color-primary-fg); }
  .badge.lex   { background:var(--color-ok-tint);      color:var(--color-ok-fg); }
  .badge.ok    { background:var(--color-ok-tint);      color:var(--color-ok-fg); }
  .badge.amb   { background:var(--color-warn-tint);    color:var(--color-warn-fg); }
  .badge.flag  { background:var(--color-warn-tint);    color:var(--color-warn-fg); }
  .badge.below { background:var(--color-danger-tint);  color:var(--color-danger-fg); }

  /* ── Detail table ── */
  .detail { margin-top:var(--space-md); border-top:1px solid var(--color-border); padding-top:var(--space-md); display:none; }
  .detail.open { display:block; }
  .detail table { border-collapse:collapse; width:100%; font-size:var(--text-sm); }
  .detail td { padding:var(--space-xs) var(--space-sm) var(--space-xs) 0; vertical-align:top; border-bottom:1px solid var(--color-border); }
  .detail td.k { color:var(--color-ink-2); white-space:nowrap; font-family:var(--font-mono); }
  .detail pre { margin:0; white-space:pre-wrap; word-break:break-word; font-family:var(--font-mono); color:var(--color-ink); }

  .sep { color:var(--color-ink-3); font-size:var(--text-sm); margin:var(--space-md) 0 var(--space-sm);
         border-top:1px dashed var(--color-border-2); padding-top:var(--space-xs); }
  .err { color:var(--color-danger-fg); }

  /* ── Introspect ── */
  details.introspect { margin-top:var(--space-2xl); border-top:1px solid var(--color-border); padding-top:var(--space-md); }
  details.introspect summary { cursor:pointer; color:var(--color-ink-2); }
  details.introspect summary:hover { color:var(--color-ink); }
  .introspect .controls { margin-top:var(--space-md); }
  .introspect .out { font-size:var(--text-sm); }
  .introspect .row { padding:var(--space-xs) 0; border-bottom:1px solid var(--color-border); }
  .introspect .row .loc { font-weight:600; }
  .empty { color:var(--color-ink-3); padding:var(--space-sm) 0; }
</style>
</head>
<body>
<header>
  <div class="bar">
    <h1>kawasemi-indexer 索引診断コンソール<span class="brand-dot">.</span></h1>
    <button class="theme-toggle" id="themeToggle" type="button" aria-label="ライト/ダーク切替" title="ライト/ダーク切替">◐</button>
  </div>
  <div id="status">索引情報を読み込み中…</div>
</header>
<main>
  <div class="controls">
    <input id="q" placeholder="臨床的コンテキストを入力（例: 芽球割合）" autofocus>
    <select id="kind"><option value="">全 kind</option></select>
    <select id="conn"><option value="">全 conn</option></select>
    <label class="lbl">top-k <input id="topk" type="number" value="8" min="1"></label>
    <label class="lbl">min_score <input id="minscore" type="number" value="0.0" step="0.05"></label>
    <button class="btn--primary" id="go">検索</button>
  </div>
  <div class="meta-line" id="meta"></div>
  <div id="results"></div>

  <details class="introspect">
    <summary>内省（:grep / :why）— 索引にそもそも在るか・なぜ上位に来ないか</summary>
    <div class="controls">
      <input id="iterm" placeholder="語・カラム名・unit_id（例: PATIENT_ID）">
      <button class="btn--secondary" id="grepBtn">:grep（存在スキャン）</button>
      <button class="btn--secondary" id="whyBtn">:why（直近クエリでの順位）</button>
    </div>
    <div class="out" id="introOut"></div>
  </details>
</main>
<script>
const $ = (id) => document.getElementById(id);
let lastQuery = "";

/* トークンは起動時に表示される URL の `#token=...` で渡る。fragment はサーバへ送られない。
 * 読んだら URL から消し、再読込に備えて sessionStorage（タブを閉じれば消える）に置く。 */
let TOKEN = "";
try {
  const m = /[#&]token=([^&]+)/.exec(location.hash);
  if (m) {
    TOKEN = decodeURIComponent(m[1]);
    sessionStorage.setItem("kawasemi-indexer-token", TOKEN);
    history.replaceState(null, "", location.pathname + location.search);
  } else {
    TOKEN = sessionStorage.getItem("kawasemi-indexer-token") || "";
  }
} catch (e) {}
const authHeaders = () => (TOKEN ? {"authorization": "Bearer " + TOKEN} : {});

async function jpost(url, body) {
  const r = await fetch(url, {method:"POST", headers:{"content-type":"application/json", ...authHeaders()}, body:JSON.stringify(body)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
  return data;
}
async function jget(url) {
  const r = await fetch(url, {headers: authHeaders()});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
  return data;
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));

async function loadStatus() {
  try {
    const s = await jget("/api/status");
    const kc = s.kind_counts || {};
    const kinds = Object.keys(kc);
    const parts = kinds.map(k => `${(s.kind_labels||{})[k]||k}=${kc[k]}`).join(" / ");
    $("status").innerHTML =
      `索引: <b>${esc(s.index_dir||"-")}</b> &nbsp; model=<b>${esc(s.model)}</b> ` +
      `dim=<b>${esc((s.build_info||{}).dim)}</b> units=<b>${esc(s.unit_count)}</b> &nbsp; ${esc(parts)}`;
    // kind フィルタを動的生成
    const ksel = $("kind");
    kinds.forEach(k => {
      const o = document.createElement("option");
      o.value = k; o.textContent = `${(s.kind_labels||{})[k]||k} (${kc[k]})`;
      ksel.appendChild(o);
    });
    // connection フィルタ
    const csel = $("conn");
    Object.keys(s.connection_counts||{}).sort().forEach(c => {
      const o = document.createElement("option");
      o.value = c; o.textContent = `${c} (${s.connection_counts[c]})`;
      csel.appendChild(o);
    });
    if (!s.is_searchable) $("status").innerHTML += ` <span class="err">（検索不可: モデル不一致）</span>`;
  } catch (e) {
    $("status").innerHTML = `<span class="err">索引情報の取得に失敗: ${esc(e.message)}</span>`;
  }
}

function badges(d) {
  const b = [];
  (d.value_dict_hits||[]).forEach(v => b.push(`<span class="badge vd">← ${esc(v)}</span>`));
  (d.lexical_hits||[]).forEach(v => b.push(`<span class="badge lex">↳ ${esc(v)}</span>`));
  if (!(d.lexical_hits||[]).length && !(d.value_dict_hits||[]).length)
    b.push(`<span class="badge">意味的近接のみ</span>`);
  if (d.ambiguous) b.push(`<span class="badge amb">⚠ambiguous</span>`);
  (d.quality_flags||[]).forEach(f => b.push(`<span class="badge flag">${esc(f)}</span>`));
  return b.join("");
}

function card(h, minScore, showSep) {
  const sep = showSep ? `<div class="sep">--- min_score = ${esc(minScore)} 以下 ---</div>` : "";
  return sep + `<div class="card" data-uid="${esc(h.unit_id)}">
    <div class="head">
      <span class="rank">[${h.cand_id}]</span>
      <span class="score">${h.score.toFixed(4)}</span>
      <span class="kind">${esc(h.kind)}</span>
      <span class="loc">${esc(h.location)}</span>
      <span class="label">${esc(h.label)}</span>
    </div>
    <div class="badges">${badges(h.diagnostics||{})}</div>
    <div class="detail" id="d-${esc(h.unit_id)}"></div>
  </div>`;
}

async function doSearch() {
  const q = $("q").value.trim();
  if (!q) return;
  lastQuery = q;
  $("meta").textContent = "検索中…";
  $("results").innerHTML = "";
  try {
    const body = {
      query: q,
      top_k: parseInt($("topk").value || "8", 10),
      min_score: parseFloat($("minscore").value || "0"),
      kind: $("kind").value || null,
      connection_id: $("conn").value || null,
    };
    const res = await jpost("/api/search", body);
    let meta = `上位 ${res.shown} 件 / 全 ${res.total} 件（mode=${res.mode}, min_score=${res.min_score}）`;
    if (res.rewrite) meta += ` ／ ↳ クエリ展開 ${res.rewrite.expansions.length}語`;
    if ((res.ref_masters||[]).length) meta += ` ／ ↳ 参照リンク発動（候補${res.ref_candidate_count}列）`;
    $("meta").textContent = meta;
    if (!res.hits.length) { $("results").innerHTML = `<div class="empty">候補なし</div>`; return; }
    let sepDrawn = false, html = "";
    for (const h of res.hits) {
      const below = h.diagnostics && h.diagnostics.below_min_score;
      const showSep = below && !sepDrawn;
      if (showSep) sepDrawn = true;
      html += card(h, res.min_score, showSep);
    }
    $("results").innerHTML = html;
  } catch (e) {
    $("meta").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
}

async function toggleDetail(uid) {
  const el = $("d-" + uid);
  if (!el) return;
  if (el.classList.contains("open")) { el.classList.remove("open"); return; }
  if (!el.dataset.loaded) {
    try {
      const u = await jget("/api/show/" + encodeURIComponent(uid));
      let rows = "";
      for (const [k, v] of Object.entries(u)) {
        const val = (typeof v === "object") ? JSON.stringify(v, null, 2) : String(v);
        rows += `<tr><td class="k">${esc(k)}</td><td><pre>${esc(val)}</pre></td></tr>`;
      }
      el.innerHTML = `<table>${rows}</table>`;
      el.dataset.loaded = "1";
    } catch (e) { el.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  }
  el.classList.add("open");
}

async function doGrep() {
  const term = $("iterm").value.trim();
  if (!term) return;
  $("introOut").textContent = "スキャン中…";
  try {
    const r = await jpost("/api/grep", {term, kind: $("kind").value || null, connection_id: $("conn").value || null});
    if (!r.count) { $("introOut").innerHTML = `<div class="empty">一致なし</div>`; return; }
    let html = `<div class="meta-line">${r.count} 件一致</div>`;
    for (const m of r.matches)
      html += `<div class="row"><span class="kind">${esc(m.kind)}</span> <span class="loc">${esc(m.location)}</span> — ${esc(m.logical_name||"—")} <span class="label">${esc(m.unit_id)}</span></div>`;
    $("introOut").innerHTML = html;
  } catch (e) { $("introOut").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

async function doWhy() {
  const term = $("iterm").value.trim();
  if (!term) return;
  $("introOut").textContent = "照合中…";
  try {
    const r = await jpost("/api/why", {term, query: lastQuery || null,
      top_k: parseInt($("topk").value || "8", 10), kind: $("kind").value || null, connection_id: $("conn").value || null});
    if (!r.exists) { $("introOut").innerHTML = `<div class="empty">'${esc(term)}' は索引に見つかりません</div>`; return; }
    let html = `<div class="meta-line">索引に ${r.count} 件存在` +
      (r.ranked ? `（直近クエリ "${esc(r.query)}" / 全 ${esc(r.total_ranked)} 件中の順位）` : `（順位は検索後に表示）`) + `</div>`;
    for (const m of r.matches) {
      let rank = "";
      if (r.ranked) {
        if (m.rank == null) rank = ` <span class="badge below">フィルタ対象外</span>`;
        else rank = ` <span class="score">第${m.rank}位</span> ${m.score.toFixed(4)} ` +
          (m.in_topk ? `<span class="badge lex">top-k 内</span>` : `<span class="badge amb">top-k 外</span>`);
      }
      html += `<div class="row"><span class="kind">${esc(m.kind)}</span> <span class="loc">${esc(m.location)}</span>${rank}</div>`;
    }
    $("introOut").innerHTML = html;
  } catch (e) { $("introOut").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

$("go").addEventListener("click", doSearch);
$("q").addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });
$("results").addEventListener("click", e => {
  const c = e.target.closest(".card");
  if (c) toggleDetail(c.dataset.uid);
});
$("grepBtn").addEventListener("click", doGrep);
$("whyBtn").addEventListener("click", doWhy);
$("iterm").addEventListener("keydown", e => { if (e.key === "Enter") doGrep(); });

// テーマトグル（本体と同じ localStorage キー 'kawasemi-theme'）
function syncThemeToggle() {
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  $("themeToggle").textContent = dark ? "☀" : "◐";
}
$("themeToggle").addEventListener("click", () => {
  const root = document.documentElement;
  const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
  if (next === "dark") root.setAttribute("data-theme", "dark");
  else root.removeAttribute("data-theme");
  try { localStorage.setItem("kawasemi-theme", next); } catch (e) {}
  syncThemeToggle();
});
syncThemeToggle();
loadStatus();
</script>
</body>
</html>
"""
