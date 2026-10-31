"""推論プロバイダの識別のユニットテスト。

`provider_of` は**設定値ではなく client 自身**から名前を取る。設定を別に持つと、client を
差し替えたのに設定を直し忘れた記録が「Ollama で測った」と嘘をつくためである。ここで固定する
のはその契約——名乗らない client は既定、名乗る client はその値、名乗り方が壊れていれば落ちる。
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from kawasemi.ai_input.llm_provider import (
    DEFAULT_PROVIDER,
    DIGEST_MAX_LENGTH,
    DIGEST_REASON_MAX_LENGTH,
    PROVIDER_MAX_LENGTH,
    design_llm_model,
    make_design_llm_client,
    model_digest_of,
    provider_of,
    validate_provider_name,
)


@pytest.mark.parametrize("value", ["legacy", "o-llama", "vllm2", ""])
def test_an_unknown_llm_provider_fails_instead_of_falling_back_to_ollama(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """知らない `LLM_PROVIDER` は**黙って ollama へ倒さず**落ちる。

    倒すと、別のプロバイダを書いたつもりの設定が Ollama での測定として記録され、来歴の
    `provider` も "ollama" になる（記録が嘘をつく）。以前あったプロバイダを外したあとも、
    その名前がこの検証で弾かれることをここで固定する。
    """
    from kawasemi.core.config import settings

    monkeypatch.setattr(settings, "llm_provider", value)
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        make_design_llm_client()
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        design_llm_model()


def test_ollama_is_the_provider_that_works(monkeypatch: pytest.MonkeyPatch) -> None:
    from kawasemi.core.config import settings

    monkeypatch.setattr(settings, "llm_provider", " Ollama ")
    assert design_llm_model() == settings.ollama_model
    assert make_design_llm_client() is not None


def test_a_client_without_a_name_is_the_default() -> None:
    """`ollama.AsyncClient` は `provider_name` を持たないので既定になる。

    移行前に保存された記録はすべて Ollama で生成されているので、既定値が "ollama" である
    ことは事実と一致する（migration の server_default と同じ根拠）。
    """
    assert provider_of(SimpleNamespace()) == DEFAULT_PROVIDER
    assert DEFAULT_PROVIDER == "ollama"


def test_a_client_that_names_itself_wins() -> None:
    """アダプタが名乗った名前がそのまま記録される。"""
    assert provider_of(SimpleNamespace(provider_name="stub")) == "stub"


@pytest.mark.parametrize("bad", [None, "", "   ", 42, object()])
def test_a_broken_name_falls_back_instead_of_writing_null(bad: object) -> None:
    """名乗り方が壊れていても NOT NULL 列に入る値を返すこと。

    `provider` 列は NOT NULL である。ここで `None` や `""` を通すと、**LLM 呼び出しは
    成功したのに来歴の保存だけが落ちる**という最も分かりにくい失敗になる。
    """
    got = provider_of(SimpleNamespace(provider_name=bad))
    assert got == DEFAULT_PROVIDER


def test_a_name_is_trimmed() -> None:
    assert provider_of(SimpleNamespace(provider_name="  stub  ")) == "stub"


def test_provider_of_never_raises() -> None:
    """**`provider_of` は例外を投げない。** レビューで、投げる設計が測定を壊すと実測された。

    呼ばれる位置が2つとも致命的だった。`executor` では生成成功後の trace 組み立て中なので
    例外は**抽出値と生出力の両方を捨て**、`chat_json` では送信前なので例外が
    `chat_json_chunked` に `failed`（送った）として計上され、**1件も送っていないのに全項目が
    「送ったが結果にならなかった」**になる（送信前の失敗を送信後の失敗と取り違える誤帰属）。

    長すぎる名前はアダプタの実装の誤りで、構築時に `validate_provider_name` が落とす。
    """
    long_name = "x" * (PROVIDER_MAX_LENGTH + 10)
    got = provider_of(SimpleNamespace(provider_name=long_name))
    assert len(got) == PROVIDER_MAX_LENGTH, "列に入らない値を返してはいけない"


def test_a_name_too_long_is_refused_at_construction_time() -> None:
    """**設定の誤りは設定を読むときに落ちる。** 多数の項目を生成する途中で落ちてよいものではない。

    切り詰めて通さないのは、"model-e-flash-via-some-server" のような別名が生まれると、
    後から集計する側が同じプロバイダか別物か判断できなくなるためである。
    """
    with pytest.raises(ValueError, match="長すぎる"):
        validate_provider_name("x" * (PROVIDER_MAX_LENGTH + 1))
    with pytest.raises(ValueError, match="空"):
        validate_provider_name("   ")
    assert validate_provider_name("  stub  ") == "stub"


def test_the_column_length_matches_the_model() -> None:
    """`PROVIDER_MAX_LENGTH` が DB 列の長さと一致していること。

    片方だけ変えると、通るはずの名前が保存時に落ちる（あるいは検査が甘くなる）。
    """
    from kawasemi.ai_input.models import DesignLlmCall

    column = DesignLlmCall.__table__.c.provider
    assert column.type.length == PROVIDER_MAX_LENGTH
    assert column.nullable is False


# --------------------------------------------------------------------------- #
# モデルのダイジェスト
#
# タグだけでは実装が特定できない。`ollama pull` で上流が更新されても、ローカルで同名タグを
# 作り直しても、記録は同じ文字列のままである（「`.env` を書き換えるだけで
# モデルが変わる」問題の残り）。ここで固定するのは、**取れたら残す・取れなければ理由を残す・
# 決して例外を投げない**という契約である。
# --------------------------------------------------------------------------- #


class _Listing:
    """`list()` を持つ client（Ollama 相当）。呼ばれた回数を数える。"""

    def __init__(self, entries: object, *, provider_name: str | None = None) -> None:
        self._entries = entries
        self.calls = 0
        if provider_name is not None:
            self.provider_name = provider_name

    async def list(self) -> object:
        self.calls += 1
        return self._entries


def _models(*entries: object) -> SimpleNamespace:
    return SimpleNamespace(models=list(entries))


@pytest.mark.asyncio
async def test_the_digest_comes_from_the_model_listing() -> None:
    """ダイジェストは `list()`（`/api/tags`）から引くこと。

    **`/api/show` は digest を持たない**（`ShowResponse` のフィールドは `modified_at` /
    `template` / `modelfile` / `license` / `details` / `modelinfo` / `parameters` /
    `capabilities`。実測）。経路を `show` にすると常に null になる。
    """
    client = _Listing(_models(SimpleNamespace(model="model-a:12b", digest="sha256:abc123")))
    assert await model_digest_of(client, "model-a:12b") == ("sha256:abc123", None)


@pytest.mark.asyncio
async def test_a_tag_without_a_colon_means_latest() -> None:
    """タグを省いた指定は `:latest` を指すこと（Ollama の規則）。"""
    client = _Listing(_models(SimpleNamespace(model="model-a:latest", digest="sha256:def")))
    assert await model_digest_of(client, "model-a") == ("sha256:def", None)


@pytest.mark.asyncio
async def test_a_client_without_a_listing_records_the_reason() -> None:
    """`list()` を持たない client は、**理由つきの null** になること。

    OpenAI 互換のアダプタがこれである。`/v1/models` が返す項目には
    **digest 相当のフィールドが無い**。

    **取れないことは欠陥ではない**ので、黙って null にせず理由を残す——「宣言していない
    （両方 null）」と「引いたが取れなかった」を区別できなくすると、後から記録を読む人が
    「この実行はダイジェストを記録していたのか」を答えられない。
    """
    digest, reason = await model_digest_of(SimpleNamespace(provider_name="stub"), "model-e")
    assert digest is None
    assert reason is not None and "stub" in reason and "digest" in reason


@pytest.mark.asyncio
async def test_a_failing_listing_never_raises() -> None:
    """取得の失敗が生成を壊さないこと（`provider_of` と同じ理由）。

    多数の項目を生成する途中で推論サーバへの1回の往復が失敗したときに、成功した生成を捨てる
    理由は無い。**来歴の記録が測定を壊してはならない**（`llm_provider` の docstring が
    実測つきで書いている失敗の形）。

    `TimeoutError` を投げる client も**型名つきの理由**になる。以前は
    `asyncio.wait_for` の `TimeoutError` と同じ `except` で受けていたため、即座に失敗した
    client でも理由が「10 秒で応答が無い」になっていた。
    """

    class _Broken:
        async def list(self) -> object:
            raise ConnectionError("接続できない")

    digest, reason = await model_digest_of(_Broken(), "model-a:12b")
    assert digest is None
    assert reason is not None and "取得に失敗" in reason and "ConnectionError" in reason


@pytest.mark.asyncio
async def test_a_model_missing_from_the_listing_records_the_reason() -> None:
    """一覧に無いモデルは理由を残すこと（別のモデルの digest を拾わない）。"""
    client = _Listing(_models(SimpleNamespace(model="other:1b", digest="sha256:zzz")))
    digest, reason = await model_digest_of(client, "model-a:12b")
    assert digest is None
    assert reason is not None and "model-a:12b" in reason


@pytest.mark.asyncio
async def test_an_entry_without_a_digest_records_the_reason() -> None:
    """一覧にはあるが digest が空なら、空文字を残さず理由にすること。"""
    client = _Listing(_models(SimpleNamespace(model="model-a:12b", digest="  ")))
    digest, reason = await model_digest_of(client, "model-a:12b")
    assert digest is None
    assert reason is not None and "digest が空" in reason


@pytest.mark.asyncio
async def test_the_digest_is_fetched_once_per_client() -> None:
    """同じ client・同じモデルでは往復が1回で済むこと。

    `chat_json_chunked` は1要求の中で何度も呼ぶので、毎回引くと往復が項目数に比例する。
    **キャッシュは client 単位**にする——プロセス全体で1回にすると、実行の間にタグの
    中身が差し替わったことが記録に出なくなる（それを検出するための記録である）。
    """
    client = _Listing(_models(SimpleNamespace(model="model-a:12b", digest="sha256:abc")))
    assert await model_digest_of(client, "model-a:12b") == ("sha256:abc", None)
    assert await model_digest_of(client, "model-a:12b") == ("sha256:abc", None)
    assert client.calls == 1


@pytest.mark.asyncio
async def test_an_adapter_can_provide_the_digest_itself() -> None:
    """アダプタが `model_digest()` を持つならそれを使うこと（一覧より優先）。"""

    class _Adapter:
        provider_name = "stub"

        async def model_digest(self, model: str) -> str:
            return f"sha256:{model}"

        async def list(self) -> object:  # 使われない側
            raise AssertionError("model_digest() があるなら list() は引かない")

    assert await model_digest_of(_Adapter(), "model-e") == ("sha256:model-e", None)


@pytest.mark.asyncio
async def test_dict_shaped_listings_are_read_too() -> None:
    """`list()` が dict を返す版でも読めること（client の版差を吸収する）。"""
    client = _Listing({"models": [{"name": "model-a:12b", "digest": "sha256:dict"}]})
    assert await model_digest_of(client, "model-a:12b") == ("sha256:dict", None)


@pytest.mark.asyncio
async def test_the_digest_is_clamped_to_the_column_length() -> None:
    """ダイジェストが列に入る長さへ収まること。

    **経路に依らず掛ける。** 長すぎる値をそのまま渡すと、postgres 構成では保存が例外になり
    `save_design_llm_calls_safely` がそれを飲む——**そのロールの来歴が丸ごと落ち、確認書
    からも消える**（「AI を使っていない」と「記録に失敗した」が区別できなくなる）。
    """
    client = _Listing(_models(SimpleNamespace(model="m", digest="d" * 5000)))
    digest, reason = await model_digest_of(client, "m")
    assert digest is not None and len(digest) == DIGEST_MAX_LENGTH
    assert reason is None


@pytest.mark.asyncio
async def test_the_reason_is_clamped_on_every_path() -> None:
    """理由も列に入る長さへ収まること（例外経路だけでなく）。

    初版は `except` の中でしか打ち切っていなかったので、長いモデル名を渡すと
    「一覧に … が無い」の理由が列長（255）を超えた（実測416文字）。
    """
    client = _Listing(_models())
    digest, reason = await model_digest_of(client, "m" * 400)
    assert digest is None
    assert reason is not None and len(reason) <= DIGEST_REASON_MAX_LENGTH


def test_the_clamp_lengths_match_the_columns() -> None:
    """打ち切り長が DB の列長と一致すること（`PROVIDER_MAX_LENGTH` と同じ形の検査）。

    定数だけ直して列を直し忘れる（またはその逆）と、**保存時に落ちて来歴が消える**。
    """
    from kawasemi.ai_input.models import DesignLlmCall

    assert DesignLlmCall.__table__.c.model_digest.type.length == DIGEST_MAX_LENGTH
    assert (
        DesignLlmCall.__table__.c.model_digest_unavailable.type.length == DIGEST_REASON_MAX_LENGTH
    )


@pytest.mark.asyncio
async def test_the_real_digest_shape_is_read_as_is() -> None:
    """実測の形（**64桁 hex・`sha256:` 接頭辞なし**）をそのまま読むこと。

    Ollama 0.6.1 の `/api/tags` が返すのはこの形である（実サーバで実測）。経路を
    `details.digest` 等へ差し替える退行を、フィクスチャの形で捕まえる錨にする。
    """
    real = "4eb23ef187e2c5461e824f7c0d1f0c9b2f2fbb5a7c58b7a86e9a1d0f3c4b5a69"
    assert len(real) == 64
    client = _Listing(_models(SimpleNamespace(model="model-a:12b", digest=real)))
    assert await model_digest_of(client, "model-a:12b") == (real, None)


@pytest.mark.asyncio
async def test_a_non_string_digest_is_not_reported_as_empty() -> None:
    """型違いを「空である」と書かないこと。

    「空である」と書くと、後から記録を読む人はサーバが空文字を返したと読む。実際に
    入っていたのが数値なら、それは別の壊れ方である。
    """
    client = _Listing(_models(SimpleNamespace(model="m", digest=12345)))
    digest, reason = await model_digest_of(client, "m")
    assert digest is None
    assert reason is not None and "文字列でない" in reason


@pytest.mark.asyncio
async def test_a_hanging_listing_is_cut_by_the_timeout() -> None:
    """固まる `list()` を上限時間で切ること。

    ダイジェストの取得は生成の全体デッドライン（`llm_total_timeout`）の**外**で走る。
    独自の上限が無いと、TCP は受けるが `/api/tags` を返さないサーバで
    `settings.ollama_timeout`（環境によっては900秒）だけ無音になる。
    """
    import kawasemi.ai_input.llm_provider as provider_mod

    class _Hanging:
        async def list(self) -> object:
            await asyncio.sleep(3600)
            raise AssertionError("到達しない")

    original = provider_mod.DIGEST_TIMEOUT_SECONDS
    provider_mod.DIGEST_TIMEOUT_SECONDS = 0.05
    try:
        digest, reason = await model_digest_of(_Hanging(), "m")
    finally:
        provider_mod.DIGEST_TIMEOUT_SECONDS = original
    assert digest is None
    assert reason is not None and "応答が無い" in reason


@pytest.mark.asyncio
async def test_a_client_without_a_listing_says_it_is_the_client() -> None:
    """理由が「client が一覧の経路を持たない」であること（サーバの性質と混ぜない）。

    OpenAI 互換のサーバは `/v1/models` を**持っている**。持っていないのは digest 相当の
    フィールドで、アダプタがその一覧を `list()` として露出していないだけである。
    **サーバが一覧を持たない**と書くと、記録を読む人が誤った答えを受け取る。
    """
    digest, reason = await model_digest_of(SimpleNamespace(provider_name="stub"), "model-e")
    assert digest is None
    assert reason is not None and "client がモデル一覧の経路を持たない" in reason


@pytest.mark.asyncio
async def test_a_client_raising_timeout_is_not_reported_as_our_deadline() -> None:
    """client 自身の `TimeoutError` を、**こちらのデッドライン**と混同しないこと。

    どちらも digest が null になる点は同じで、そこは正しい。読み手が誤るのは
    「10秒待ったのか、即座に失敗したのか」である。記録として
    `model_digest_unavailable` に残る文字列なので、字面を分ける。
    """
    import kawasemi.ai_input.llm_provider as provider_mod

    class _Instant:
        async def list(self) -> object:
            raise TimeoutError("接続先が即座に打ち切った")

    started = time.perf_counter()
    digest, reason = await model_digest_of(_Instant(), "m")
    elapsed = time.perf_counter() - started

    assert digest is None
    assert reason is not None
    assert "TimeoutError" in reason, reason
    assert "秒で応答が無い" not in reason, reason
    # 実際に待っていないことも見る（上限を待ってから同じ理由を作る実装を許さない）
    assert elapsed < provider_mod.DIGEST_TIMEOUT_SECONDS / 2


@pytest.mark.asyncio
async def test_our_own_deadline_still_says_how_long_it_waited() -> None:
    """こちらのデッドラインで切れたときは、待った秒数を理由に残すこと。

    区別を入れたことで**上限側の理由が出なくなる**（両方が型名つきになる）退行を防ぐ。
    """
    import kawasemi.ai_input.llm_provider as provider_mod

    class _Hanging:
        async def list(self) -> object:
            await asyncio.sleep(3600)
            raise AssertionError("到達しない")

    original = provider_mod.DIGEST_TIMEOUT_SECONDS
    provider_mod.DIGEST_TIMEOUT_SECONDS = 0.05
    try:
        digest, reason = await model_digest_of(_Hanging(), "m")
    finally:
        provider_mod.DIGEST_TIMEOUT_SECONDS = original

    assert digest is None
    assert reason is not None and "秒で応答が無い" in reason
    assert "TimeoutError" not in reason, reason
