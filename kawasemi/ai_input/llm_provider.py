"""推論プロバイダの識別。

**モデル名だけでは、どの実装が生成したかを特定できない。** `OLLAMA_MODEL` は環境変数で
上書きでき（コードのハッシュにも現れない）、同じモデル名を別の推論エンジンが名乗ることもできる。来歴に `provider` を別に
持たないと、後から記録を読む人は「その測定値がどのエンジンで出たか」を答えられない。

設計ロール（`llm_json` → `ai_input_design_llm_calls.provider`）と実行ロール
（`executor` → evidence trace の `llm.provider`）の**両方**がここを使う。両ロールは
意図的に互いを import しない（PHI の扱いが違うため層として分けてある）ので、共有する
必要のあるものはこの中立なモジュールに置く。
"""

from __future__ import annotations

import asyncio
from typing import Any

import ollama

from kawasemi.ai_input.vllm_client import VllmClient
from kawasemi.core.config import settings

# プロバイダ名を名乗らない client（`ollama.AsyncClient`）はこれになる。移行前の記録と
# 値を揃えるため、既定は "ollama" である。
DEFAULT_PROVIDER = "ollama"

# `LLM_PROVIDER` に書ける値。
PROVIDER_OLLAMA = "ollama"
PROVIDER_VLLM = "vllm"
SUPPORTED_PROVIDERS = (PROVIDER_OLLAMA, PROVIDER_VLLM)

# `provider` 列の長さ（`kawasemi/ai_input/models.py` の DesignLlmCall と揃える）。
PROVIDER_MAX_LENGTH = 32


def provider_of(client: Any) -> str:
    """``client`` が名乗るプロバイダ名を返す。

    **設定値ではなく、呼び出しに使う client 自身から取る。** `LLM_PROVIDER` のような設定を
    別に持つと、client を差し替えたのに設定を直し忘れた記録が「Ollama で測った」と嘘をつく。
    実際に喋る相手が名乗るなら、その経路ではずれない。

    `ollama.AsyncClient` は `provider_name` を持たないので `DEFAULT_PROVIDER` になる。
    別プロバイダのアダプタは `provider_name` を定義する責任を負う。

    空文字・非文字列は名乗っていないものとして扱う。`provider` 列は NOT NULL なので、
    ここで `None` や `""` を通すと保存時に落ちる。

    **この関数は例外を投げない。** 初版は列に入らない長さで `ValueError` を投げていたが、
    それは**守るべき測定を壊した**（レビューで実測）。呼ばれる位置が2つとも致命的だった。

    - `executor` では生成が成功した後の trace 組み立て中に走るので、例外は成功した
      `llm_extract` をエラー step に化けさせ、**抽出値と生出力の両方を捨てた**（取得困難度は
      モデルが実際に答えた項目を `unclassified` の失敗として記録した）
    - `chat_json` では送信前に走るので、例外は `chat_json_chunked` に `failed`（送った）として
      計上され、**1件も送っていないのに全項目が「送ったが結果にならなかった」**になった
      （誤帰属。3状態会計 `answered + failed + unattempted` が壊れる）

    **来歴の検証が測定を壊してはならない。** 長すぎる名前はアダプタ側の実装の誤りで、
    それは**構築時**に `validate_provider_name` が落とす（設定の誤りは設定を読むときに落ちる
    べきで、生成の途中で落ちてよいものではない）。ここは列に入る値を必ず返す。
    """
    name = getattr(client, "provider_name", None)
    if not isinstance(name, str):
        return DEFAULT_PROVIDER
    name = name.strip()
    if not name:
        return DEFAULT_PROVIDER
    # 構築時の検査を通っていれば、この切り詰めは発火しない（多重防御）。
    return name[:PROVIDER_MAX_LENGTH]


# 接続先の列の長さ（`kawasemi/ai_input/models.py` の DesignLlmCall と揃える）。
ENDPOINT_MAX_LENGTH = 255


def endpoint_of(client: Any) -> str | None:
    """``client`` が喋る相手（base URL）を返す。取れなければ `None`。

    **所要時間はマシン依存なので、時間だけ残しても比較できない。** 別マシン・別モデルの
    実測値は当てはまらないが、接続先を残さないと、記録された所要時間がどのマシンの値かを
    来歴から辿れない。

    **設定値ではなく client から取る**（`provider_of` と同じ理由）。`OLLAMA_BASE_URL` を
    読むと、client を差し替えたのに設定を直し忘れた記録が別の相手を指す。

    **この関数は例外を投げない。** `provider_of` の docstring にある実測と同じで、
    来歴の検証が生成を壊してはならない。`ollama.AsyncClient` は host を
    公開していないので `_client.base_url`（httpx）を見る。私有属性なので、
    取れなくなったら黙って `None` を返す——**記録が1つ欠けるのは、測定が落ちるより軽い**。
    """
    # アダプタが公開しているならそれを使い、無ければ `ollama.AsyncClient` が包む httpx を見る。
    candidates: list[Any] = [
        getattr(client, "base_url", None),
        getattr(getattr(client, "_client", None), "base_url", None),
    ]
    for value in candidates:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text[:ENDPOINT_MAX_LENGTH]
    return None


def validate_provider_name(name: str) -> str:
    """アダプタが名乗る名前を**構築時に**検証して返す。

    `provider_of` は測定の途中で呼ばれるので例外を投げられない。代わりに、アダプタが
    自分を組み立てる時点でここを通す——**設定の誤りは設定を読むときに落ちるのが正しい**。

    Raises:
        ValueError: 空、または `provider` 列に入らない長さ

    切り詰めて通さないのは、`"model-e-flash-via-some-server"` のような別名が生まれると、
    後から集計する側が同じプロバイダか別物かを判断できなくなるためである。
    """
    stripped = name.strip()
    if not stripped:
        raise ValueError("provider_name が空である")
    if len(stripped) > PROVIDER_MAX_LENGTH:
        raise ValueError(
            f"provider_name が長すぎる（{len(stripped)} 文字 > {PROVIDER_MAX_LENGTH}）: {name!r}"
        )
    return stripped


def _selected_provider() -> str:
    """`LLM_PROVIDER` を検証して返す。

    **知らない値を黙って ollama へ倒さない。** 倒すと、別のプロバイダを書いたつもりのタイプミスが Ollama での測定として記録され、来歴の `provider` も "ollama" になる。
    `provider` を別に持って塞いだ「記録が嘘をつく」経路が設定の側から戻ってしまう。
    """
    provider = settings.llm_provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"LLM_PROVIDER に未知の値: {settings.llm_provider!r}"
            f"（使えるのは: {', '.join(SUPPORTED_PROVIDERS)}）"
        )
    return provider


def make_design_llm_client() -> Any:
    """設計ロールのテキスト生成に使う client を作る。

    返すのは「`chat(model=, messages=, format=, options=)` を持ち `.message.content` を返す
    もの」という契約だけを満たすオブジェクトである（`llm_json.chat_json` が読むのはそこだけ）。

    **vision と埋め込みはここを通さない。** `executor` の Vision 縮退と `resource_index` の
    埋め込みは Ollama 側に残る。
    """
    if _selected_provider() == PROVIDER_VLLM:
        return VllmClient(
            base_url=settings.vllm_base_url,
            model=settings.vllm_model,
            max_tokens=settings.vllm_max_tokens,
            timeout=settings.vllm_timeout,
        )
    return ollama.AsyncClient(host=settings.ollama_base_url, timeout=settings.ollama_timeout)


def design_llm_model() -> str:
    """設計ロールの既定モデル名。

    プロバイダを足すときに出所を1本にしておくためのもの。ここを通さないと、プロバイダを
    切り替えた環境で UI の既定値だけ `OLLAMA_MODEL` のままになり、**別のエンジンに Ollama の
    モデル名を渡す**形が残る。来歴には「渡したモデル名」が入るので、それは `provider` で塞いだはずの
    「記録が実体と食い違う」状態そのものである。
    """
    provider = _selected_provider()
    if provider == PROVIDER_VLLM:
        return settings.vllm_model
    return settings.ollama_model


# ダイジェストの保存上限（`DesignLlmCall.model_digest` の列長）。Ollama の `/api/tags` が
# 返すのは64桁の hex（`sha256:` 接頭辞は付かない。実測）だが、**上限は経路に依らず掛ける**
# ——長すぎる値をそのまま渡すと、postgres 構成では保存が例外になり
# `save_design_llm_calls_safely` がそれを飲んで**そのロールの来歴が丸ごと落ちる**。
DIGEST_MAX_LENGTH = 128

# ダイジェストが取れなかった理由の保存上限（`DesignLlmCall.model_digest_unavailable` の列長）。
DIGEST_REASON_MAX_LENGTH = 255

# ダイジェスト取得の上限時間（秒）。**生成の全体デッドライン（`llm_total_timeout`）の外で
# 走るので、ここに独自の上限が要る。** `settings.ollama_timeout`（既定 120秒、環境によっては
# 900秒）は httpx の1コール上限で、TCP は受けるが `/api/tags` を返さないサーバ（詰まった
# Ollama、握るプロキシ）では**生成が始まる前にその秒数だけ無音になる**（実測で再現した）。
# 一覧はローカルの読み出しなので、数秒で足りる。超えたら理由つきの null にして生成へ進む。
#
# **この上限は client インスタンス単位である**。キャッシュ（下の
# `_DIGEST_CACHE_ATTR`）が client ごとなので、設計5ロールをそれぞれ別の client で回すと
# **最悪 50 秒の無音が積む**。1つのロールの中では往復は1回で、積むのはロールをまたぐとき
# である。全体の待ちを縮めたいときは、ここを下げるか client を共有する。
DIGEST_TIMEOUT_SECONDS = 10.0

# ダイジェストを client インスタンスへ覚えておく属性名。**プロセス単位ではなく client
# 単位**にする——同じタグの中身が実行の間に差し替わったことを検出したいのに、プロセス
# 全体で1回しか引かないとその差し替えが記録に出ない。client は要求ごとに作られるので、
# 1要求の中の複数回の呼び出し（`chat_json_chunked` の各チャンク）では往復が1回で済む。
_DIGEST_CACHE_ATTR = "_kawasemi_model_digests"


def _clamped(digest: str) -> str:
    """ダイジェストを列に入る長さへ収める（経路に依らず掛ける）。"""
    return digest[:DIGEST_MAX_LENGTH]


def _reason(text: str) -> str:
    """理由を列に入る長さへ収める（経路に依らず掛ける）。"""
    return text[:DIGEST_REASON_MAX_LENGTH]


class _DigestDeadlineExceeded(Exception):
    """ダイジェスト取得が**こちらのデッドライン**で切れた。

    `asyncio.wait_for` の `TimeoutError` と、**client が自分で投げた `TimeoutError`** は
    同じ型なので、まとめて受けると即座に失敗した client でも理由が「10 秒で応答が無い」
    になる。記録された `model_digest_unavailable` を読む人が「10秒待ったのか、即座に
    失敗したのか」を取り違えるので、ここで分ける。
    """


async def _with_deadline(awaitable: Any) -> Any:
    """`DIGEST_TIMEOUT_SECONDS` で切る。切れたのがこちらのデッドラインなら区別する。

    `asyncio.timeout` は自分が発火したかを `expired()` で答える。client 由来の
    `TimeoutError` はそのまま素通しし、呼び出し側の一般の `except Exception` が
    型名つきの理由を残す。
    """
    deadline = asyncio.timeout(DIGEST_TIMEOUT_SECONDS)
    try:
        async with deadline:
            return await awaitable
    except TimeoutError:
        if deadline.expired():
            raise _DigestDeadlineExceeded from None
        raise


def _digest_from_list_entry(entry: Any) -> tuple[str | None, str]:
    """`list()` の1件からダイジェストを取り出す（版差を吸収する）。

    戻り値は `(digest, 取れなかった形の説明)`。**空と型違いを区別する**——「空である」と
    書くと、後から記録を読む人はサーバが空文字を返したと読む。
    """
    digest = getattr(entry, "digest", None)
    if digest is None and isinstance(entry, dict):
        digest = entry.get("digest")
    if digest is None:
        return None, "無い"
    if not isinstance(digest, str):
        return None, f"文字列でない（{type(digest).__name__}）"
    stripped = digest.strip()
    return (stripped, "") if stripped else (None, "空である")


def _entry_names(entry: Any) -> tuple[str, ...]:
    """`list()` の1件が名乗るモデル名（版によって `model` / `name`）。"""
    names = []
    for attr in ("model", "name"):
        value = getattr(entry, attr, None)
        if value is None and isinstance(entry, dict):
            value = entry.get(attr)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    return tuple(names)


async def model_digest_of(client: Any, model: str) -> tuple[str | None, str | None]:
    """``model`` のダイジェストと、取れなかった理由を返す。

    戻り値は `(digest, unavailable_reason)` で、**どちらか一方だけが値を持つ**。
    タグだけでは実装が特定できない——`ollama pull` で上流が更新されても、ローカルで同名
    タグを作り直しても、記録は同じ文字列のままである（「`.env` を書き換えるだけで
    モデルが変わる」問題の残りである）。

    取得経路は3つを順に試す。

    1. client が `model_digest(model)` を持つなら、それを await する（アダプタの明示的な提供）
    2. client が `list()` を持つなら、その一覧から名前で引く（Ollama。**ダイジェストは
       `/api/tags` にしか無い**——`/api/show` の `ShowResponse` は `digest` を持たない。実測）
    3. どちらも無ければ「公開していない」を理由として返す

    **この関数は例外を投げない。** 理由は `provider_of` と同じで、来歴の記録が測定を
    壊してはならないためである（一連の生成の途中で推論サーバへの1回の往復が失敗した
    ときに、成功した生成を捨てる理由は無い）。取得できなかったことは**理由つきの null**
    として残るので、「ダイジェストを記録していない窓の記録」とも「空文字」とも区別できる。

    **フェイルクローズにしない判断の限界**: ダイジェストが null の実行は、同じタグで
    中身が違うモデルを使った実行と区別できない。区別が要る測定では、記録側で
    `model_digest is null` の行を除くか、別に測り直す必要がある。
    """
    cache: dict[str, tuple[str | None, str | None]] = getattr(client, _DIGEST_CACHE_ATTR, {})
    if model in cache:
        return cache[model]

    result = await _fetch_model_digest(client, model)
    try:
        cache[model] = result
        setattr(client, _DIGEST_CACHE_ATTR, cache)
    except Exception:  # pragma: no cover - 属性を持てない client でも記録は続ける
        pass
    return result


async def _fetch_model_digest(client: Any, model: str) -> tuple[str | None, str | None]:
    """`model_digest_of` の本体（キャッシュを挟まない）。例外は投げない。"""
    wanted = model.strip()
    if not wanted:
        return None, "モデル名が空である"

    provider = provider_of(client)
    try:
        explicit = getattr(client, "model_digest", None)
        if callable(explicit):
            digest = await _with_deadline(explicit(wanted))
            if isinstance(digest, str) and digest.strip():
                return _clamped(digest.strip()), None
            return None, _reason(f"{provider} の model_digest() がダイジェストを返さなかった")

        listing = getattr(client, "list", None)
        if not callable(listing):
            # **client が一覧の経路を実装していない**場合はここに来る（OpenAI 互換のアダプタなど）。
            # `/v1/models` には digest フィールドが無い。**取れないことは欠陥ではない**ので
            # 理由を残す。
            return None, _reason(
                f"{provider} の client がモデル一覧の経路を持たない（digest を引けない）"
            )

        response = await _with_deadline(listing())
        entries = getattr(response, "models", None)
        if entries is None and isinstance(response, dict):
            entries = response.get("models")
        # タグを省いた指定（`model-a`）は `:latest` を指す。
        candidates = {wanted} | ({f"{wanted}:latest"} if ":" not in wanted else set())
        for entry in entries or ():
            if candidates & set(_entry_names(entry)):
                digest, malformed = _digest_from_list_entry(entry)
                if digest:
                    return _clamped(digest), None
                # **空と型違いを混ぜない。** 「空である」と書くと、後から記録を読む人が
                # サーバの応答が空だったと読む（実際は数値等が入っていた）。
                return None, _reason(f"{provider} の一覧の {wanted} の digest が{malformed}")
        return None, _reason(f"{provider} の一覧に {wanted} が無い")
    except _DigestDeadlineExceeded:
        # **固まる形をここで切る。** 生成の全体デッドラインの外なので、待ち続けると
        # 生成が始まらないまま無音になる（実測）。
        # **client 自身が投げた `TimeoutError` はここに来ない**。同じ型でも
        # 「10秒待った」と「即座に失敗した」は別の事実で、記録を読む人はそれを区別する。
        return None, _reason(
            f"取得に失敗: {DIGEST_TIMEOUT_SECONDS:.0f} 秒で応答が無い（{provider}）"
        )
    except Exception as exc:  # 来歴の記録が測定を壊してはならない（provider_of と同じ理由）
        return None, _reason(f"取得に失敗: {type(exc).__name__}: {exc}")
