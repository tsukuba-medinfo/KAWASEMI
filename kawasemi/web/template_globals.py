"""Jinja2 テンプレートの共通グローバルを登録するヘルパー。

各ルーターが持つ Jinja2Templates インスタンスに、テンプレート全体で
使う関数・定数を一括登録する（単一の登録点）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from jinja2.runtime import Context

from kawasemi.core.config import settings
from kawasemi.core.csrf import raw_csrf_token
from kawasemi.core.entry import current_entry
from kawasemi.models.role import RoleType

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _static_v(filename: str = "css/app.css") -> str:
    """静的ファイルのキャッシュバスト用トークン（更新時刻）を返す。

    `<link ... href=".../app.css?v={{ static_v() }}">` のように付与すると、
    CSS を更新するたびに URL が変わり、ブラウザ／プロキシが古い CSS を
    キャッシュし続けて表示が崩れるのを防げる。取得できなければ "0"。
    """
    try:
        return str(int((_STATIC_DIR / filename).stat().st_mtime))
    except OSError:
        return "0"


@pass_context
def _base_path(context: Context) -> str:
    """リバースプロキシのサブパス・プレフィックスを返す（例 "/kawasemi"、無ければ ""）。

    テンプレート内の内部絶対パスは `{{ base_path() }}/login` のように前置する。
    値はリクエストごとの scope["root_path"]（FastAPI(root_path=...) が設定）から
    取得するため、設定が空ならそのまま "/login" となり従来どおり動く。
    """
    request: Request | None = context.get("request")
    if request is None:
        return ""
    root = request.scope.get("root_path", "")
    return root if isinstance(root, str) else ""


def _csrf_token_for(request: Request) -> str | None:
    """既存の CSRF cookie から raw トークンを取り出す（全ページ共通）。

    base.html ナビの役割切替フォームなど、ルートごとに csrf_token を
    context へ渡していないテンプレートでも CSRF トークンを埋め込めるようにする。
    鮮度検証は POST 時の validate_csrf に委ねるため、読取時は期限を見ない。
    """
    return raw_csrf_token(request, secret_key=settings.csrf_secret_key)


# ナビゲーションの項目（ラベル・URL・その項目を使える役割）。
# **アクティブ役割で項目ごと消さない。** 消すと「機能が無くなった」ように見え、
# 直リンクすると 403 になるだけで、どの役割に切り替えればよいか分からなかった。
# 切替可能な役割（=保有役割）で使える項目は 🔒 付きで見せ、押せば 403 の案内画面
# 経由で1クリック切替できる（`api/main.py: role_required_handler`）。
NAV_ITEMS: list[dict[str, Any]] = [
    # 4役割。管理者は運用だけ（患者データに触れる画面は持たない）。
    {
        "label": "Questionnaire管理",
        "url": "/questionnaires",
        "roles": [RoleType.ADMIN, RoleType.DESIGNER],
    },
    {"label": "ValueSet管理", "url": "/valuesets", "roles": [RoleType.DESIGNER]},
    {"label": "データ自動収集", "url": "/ai-input/dashboard", "roles": [RoleType.DESIGNER]},
    {"label": "ユーザー管理", "url": "/admin/users", "roles": [RoleType.ADMIN]},
    {"label": "データセット管理", "url": "/datasets", "roles": [RoleType.COLLECTOR]},
    {"label": "症例割り当て", "url": "/assignments", "roles": [RoleType.COLLECTOR]},
    {"label": "入力対象一覧", "url": "/input-targets", "roles": [RoleType.COLLECTOR]},
    {"label": "回答管理", "url": "/questionnaires?mode=response", "roles": [RoleType.REVIEWER]},
    {"label": "チェック", "url": "/datasets", "roles": [RoleType.REVIEWER]},
    {
        "label": "取得メトリクス",
        "url": "/ai-input/metrics/acquisition",
        "roles": [RoleType.DESIGNER, RoleType.REVIEWER],
    },
]


def nav_items(user: Any) -> list[dict[str, Any]]:
    """ナビに出す項目を、アクティブ役割と切替可能な役割から組み立てる。

    - アクティブ役割で使える項目: そのまま押せる（`locked` = False）
    - 切替えれば使える項目: 🔒 付きで見せ、担当役割を添える（`locked` = True）
    - どの保有役割でも使えない項目: 出さない（切替えても届かないため）

    `user` は `User`（`active_role` / `switchable_role_names()`）を想定する。
    403 の案内画面はセッションを持てないので、同じ2つだけを持つ入れ物を渡してくる。
    """
    active = user.active_role
    switchable = user.switchable_role_names()
    entry = current_entry()
    items: list[dict[str, Any]] = []
    for item in NAV_ITEMS:
        roles = list(item["roles"])
        if active in roles:
            items.append({**item, "locked": False, "owner": active, "other_entry": None})
            continue
        reachable = [r for r in roles if r in switchable]
        if reachable:
            items.append({**item, "locked": True, "owner": reachable[0], "other_entry": None})
            continue
        # この入り口では使えない役割の項目: 別の入り口の URL があれば、そこへの案内として出す。
        # 無ければ出さない（押しても届かない）
        if entry.restricted and not entry.allows_any(roles):
            # 案内するのは、利用者が保有する役割の入り口だけ（持たない役割の入り口へは送らない）
            held_outside = set(user.held_role_names_outside_entry())
            hint = entry.url_for([r for r in roles if r in held_outside])
            if hint:
                items.append({**item, "locked": True, "owner": hint[0], "other_entry": hint[1]})
    return items


def register_template_globals(templates: Jinja2Templates) -> None:
    """共通グローバルをテンプレート環境に登録する。"""
    # AI機能フラグ（docs/ai_flags_spec.md）。AI操作ボタン等の表示制御に使う
    templates.env.globals["enable_ai_drafting"] = settings.enable_ai_drafting
    templates.env.globals["enable_ai_extraction"] = settings.enable_ai_extraction
    # 役割名 -> 日本語表示名（バッジ・役割切替・見出しで統一）
    templates.env.globals["role_display_name"] = RoleType.display_name
    # request から CSRF raw トークンを得る（ナビ等のフォーム用）
    templates.env.globals["csrf_token_for"] = _csrf_token_for
    # サブパス配下のプレフィックス（内部絶対パスの前置に使う。無ければ ""）
    templates.env.globals["base_path"] = _base_path
    # 静的ファイルのキャッシュバスト用トークン（app.css 更新で自動的に変わる）
    templates.env.globals["static_v"] = _static_v
    # ナビ項目（アクティブ役割で使えるもの＋切替えれば使えるもの）
    templates.env.globals["nav_items"] = nav_items
    # 入り口。ヘッダに名前を出す
    templates.env.globals["current_entry"] = current_entry
