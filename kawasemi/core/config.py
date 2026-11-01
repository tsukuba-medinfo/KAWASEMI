"""アプリケーション設定管理モジュール"""

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """アプリケーション設定クラス"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # アプリケーション基本設定
    app_name: str = Field(default="KAWASEMI", description="アプリケーション名")
    app_version: str = Field(default="0.1.0", description="アプリケーションバージョン")
    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="実行環境"
    )
    debug: bool = Field(default=False, description="デバッグモード")
    cookie_secure: bool = Field(
        default=True,
        description=(
            "認証Cookie（JWT/refresh）とCSRF CookieにSecure属性を付与するか。"
            "既定でTrue（環境ラベルに関わらず付与）。"
            "HTTPS終端が無いローカル開発でブラウザからログインを試すときのみFalseにする"
        ),
    )

    # AI機能フラグ（docs/ai_flags_spec.md）。境界基準は「患者データにLLMが触れるか」。
    # 旧 ENABLE_AI は廃止（下の reject_legacy_enable_ai で起動時に検出・拒否）。
    enable_ai_drafting: bool = Field(
        default=False,
        description=(
            "候補探索AI（設計時）: データ所在AI案・手順書生成・取得プラン作成・"
            "キー種別候補など、人間レビュー前提の設計ドラフト生成を有効にする"
        ),
    )
    enable_ai_extraction: bool = Field(
        default=False,
        description=(
            "自動構造化AI（実行時）: llm_extract / llm_judge op による"
            "患者データへのLLM適用を有効にする。確認書様式の分岐キー"
        ),
    )

    # サーバ設定
    host: str = Field(default="0.0.0.0", description="ホスト")
    port: int = Field(default=8000, description="ポート番号")
    # リバースプロキシのサブパス配下に置く場合のプレフィックス（例: "/kawasemi"）。
    # 空ならルート直下（従来どおり）。nginx 側でこのプレフィックスを strip しても
    # しなくても動くよう、FastAPI(root_path=...) とルーティングで吸収する。
    root_path: str = Field(
        default="",
        description='サブパス配下のプレフィックス（例 "/kawasemi"）。空ならルート直下',
    )

    # データベース設定
    database_url: str = Field(
        default="sqlite+aiosqlite:///./kawasemi.db",
        description="データベース接続URL",
    )

    # FHIR サーバ設定
    fhir_server_url: str = Field(default="http://localhost:8080/fhir", description="FHIRサーバURL")
    fhir_server_timeout: int = Field(default=30, description="FHIRサーバタイムアウト（秒）")

    # FHIR サーバ認証設定。本番の FHIR サーバは認証必須が一般的。
    # 認証情報はリポジトリに置かず .env（gitignore 済み）へ設定する。
    fhir_auth_mode: Literal["none", "bearer", "basic"] = Field(
        default="none",
        description="FHIRサーバ認証方式（none/bearer/basic）",
    )
    fhir_bearer_token: str = Field(
        default="", description="Bearer 認証のトークン（fhir_auth_mode=bearer で使用）"
    )
    fhir_basic_username: str = Field(
        default="", description="Basic 認証のユーザー名（fhir_auth_mode=basic で使用）"
    )
    fhir_basic_password: str = Field(
        default="", description="Basic 認証のパスワード（fhir_auth_mode=basic で使用）"
    )

    # セキュリティ設定
    secret_key: str = Field(
        ...,
        description="シークレットキー（最低32文字の暗号学的に安全なランダム文字列）",
        min_length=32,
    )
    allowed_hosts: str = Field(
        default="localhost,127.0.0.1",
        description="CORS で許可するオリジン（スキーム付き、カンマ区切り。例 https://example.org）",
    )

    # JWT認証設定
    jwt_algorithm: str = Field(default="HS256", description="JWTアルゴリズム")
    jwt_access_token_expire_minutes: int = Field(
        default=60 * 24, description="アクセストークン有効期限（分）"
    )
    jwt_cookie_name: str = Field(default="kawasemi_token", description="JWT Cookie名")
    # リフレッシュトークン。アクセストークン期限切れ後の再発行に使う。
    # DB 保存＋ローテーション式で、盗用時は失効（revoke）できる。
    jwt_refresh_token_expire_days: int = Field(
        default=14, description="リフレッシュトークン有効期限（日）"
    )
    jwt_refresh_cookie_name: str = Field(
        default="kawasemi_refresh", description="リフレッシュトークン Cookie名"
    )

    # CSRF設定
    csrf_secret_key: str = Field(
        ...,
        description="CSRFトークン用シークレットキー（SECRET_KEYとは別のキーを使用）",
        min_length=32,
    )

    # レート制限設定。**ここにあるのは実際に読まれるものだけである**。
    # `web/auth_routes.py` のログインが `core/rate_limit.py` 経由でこの値を使う。
    # 以前は `rate_limit_default` / `rate_limit_api` も並んでいたが、どのコードからも
    # 読まれていなかったので削除した（全ルートへ既定の上限を掛けるには
    # `SlowAPIMiddleware` を入れる必要があり、それは掃除の範囲を越える）。
    rate_limit_login: str = Field(default="5/minute", description="ログインレート制限")

    # アカウントロック設定: レート制限（IP単位）とは別に、
    # アカウント単位の連続失敗ロックでブルートフォースを恒久的に抑止する。
    # 注意: 単一IPからのロックを機能させるには rate_limit_login（IP単位の分あたり
    # 許可回数）を lockout_max_attempts 以上にすること。前者を小さくすると 429 が
    # 先に返り、その IP からはアカウントロックに到達しない（分散IP攻撃には有効）。
    # 以前は `rate_limit_login` がどこからも読まれておらず、ログインの上限は
    # "5/minute" のハードコードだったので、この注意書きは実態と合っていなかった。
    lockout_max_attempts: int = Field(
        default=5, description="アカウントロックまでの連続ログイン失敗回数"
    )
    lockout_duration_minutes: int = Field(
        default=30, description="アカウントロックの継続時間（分）。経過後は自動解除"
    )

    # ログ設定
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="ログレベル"
    )
    log_format: Literal["json", "text"] = Field(default="json", description="ログフォーマット")

    # Ollama AI設定
    ollama_base_url: str = Field(default="http://localhost:11434", description="OllamaサーバURL")
    ollama_model: str = Field(default="qwen3.8:27b", description="AI機能に使用するOllamaモデル名")
    ollama_vision_model: str = Field(
        default="",
        description=(
            "手書き/スキャンPDF（テキスト層なし）の読取に使うマルチモーダルOllamaモデル名。"
            "空なら ollama_model を流用する。ollama_model が画像を受け付けるとは限らないので、"
            "手書き/スキャンPDFを読むときは画像入力に対応したモデルを明示する。"
        ),
    )
    ollama_timeout: int = Field(default=120, description="Ollamaタイムアウト（秒・1コール単位）")
    llm_total_timeout: int = Field(
        default=600,
        description=(
            "チャンク分割LLM処理全体のデッドライン（秒）。"
            "**チャンク数に依らない総上限**で、N チャンクで無制限に接続を保持し続けるのを防ぐ。"
            "**項目数との関係**: 1コールの所要時間 × ceil(項目数 / chunk_size) がこの値を超えると"
            "打ち切られる（例: 70項目・chunk_size=2 なら35コール。1コール60秒なら約2,100秒が必要）。"
            "超えた場合は完了済みチャンクの結果を捨てず、未試行の件数を添えて返す。"
            "**未試行は「結果が無かった」ではない**ので、呼び出し側は区別して扱うこと"
        ),
    )
    llm_provider: str = Field(
        default="ollama",
        description=(
            "設計ロールのテキスト生成をどのエンジンで行うか。"
            "`ollama` または `vllm`（OpenAI 互換）。"
            "**既定は ollama である**——プロバイダを変えると制約デコードの機構が変わり、"
            "典拠の付与率のような測定値が横並びで比較できなくなるので、切り替えは明示的な"
            "判断として行う。"
            "**vision と埋め込みはこの設定に従わない**。"
            "`OLLAMA_VISION_MODEL` と `EMBEDDING_MODEL` は Ollama 側に残る"
        ),
    )
    vllm_base_url: str = Field(
        default="http://localhost:8100",
        description="vLLM の OpenAI 互換 API の URL。`/v1` は付けない",
    )
    vllm_model: str = Field(
        default="qwen3.8-27b",
        description=(
            "vLLM のモデル名。vLLM サーバの --served-model-name と一致させる"
            "（一致しないとリクエストが 404 になる）"
        ),
    )
    vllm_max_tokens: int = Field(
        default=32768,
        description=(
            "vLLM の応答上限。thinking も含む。DSL の生成では thinking だけで"
            "この上限に達するコールがある"
        ),
    )
    vllm_timeout: int = Field(default=3600, description="vLLM の1コールのタイムアウト（秒）")
    vision_pdf_max_pages: int = Field(
        default=20, description="Vision OCR 対象PDFの最大ページ数（超過時 step 失敗）"
    )
    vision_pdf_max_bytes: int = Field(
        default=32 * 1024 * 1024,
        description="Vision OCR 画像の合計バイト上限（超過時 step 失敗）",
    )

    # 施設固有の情報の置き場所と、実行で生まれるものの置き場所。下の *_dir / *_file を
    # 空のままにすると、この2つから既定のパスを決める（`_fill_default_paths`）。
    site_dir: str = Field(
        default="site",
        description="施設固有の情報（定義書・接続レジストリ・キー種別・知識層など）のディレクトリ",
    )
    var_dir: str = Field(
        default="var",
        description="実行で生まれるもの（索引の成果物・出力ファイル）のディレクトリ",
    )

    # DB定義書コーパス（ステップ1役割2のAI案作成で参照。施設固有）
    db_definition_dir: str = Field(
        default="",
        description="DB定義書コーパスのディレクトリ（column_index.jsonl 等を含む）",
    )

    # **試行(trial)実行で使ってよい患者ID**。カンマ区切り。
    #
    # 設計クラスタ（臨床定義責任者・取得計画設計者・データ所在管理者）は患者割り当てを持たない設計
    # なので、trial-run に `user_can_access_patient` をそのまま強制すると機能が使えなくなる。
    # 一方で以前は**設計ロールなら患者スコープ検査を一切通らなかった**ため、任意の
    # `patient_id` を指定して実患者の抽出済み臨床データと evidence を受け取れた（CWE-639）。
    #
    # **合成・ホワイトリスト患者に限る**ことで、設計者がプランを試すという目的を保ったまま
    # 穴を閉じる。空のときは trial-run も割り当てのある患者だけになる（フェイルクローズ）。
    trial_run_patient_ids: str = Field(
        default="",
        description="試行実行を許す患者IDのカンマ区切り（合成・デモ患者。空なら割り当て必須）",
    )

    # **プログラム SQL 専用の読み取り専用 DSN**。
    #
    # `_op_sql` は LLM が生成した SQL をアプリDBへ発行する。ゲートを文字列検査だけに置くと
    # SELECT から到達できる書き込み・ファイル読み書き（`SELECT ... INTO` / `pg_read_file` /
    # `INTO OUTFILE`）を取りこぼすので、**保証は接続の権限側**に置く。
    #
    # SQLite では `PRAGMA query_only` で接続スコープに閉じられるのでこの設定は不要。
    # **PostgreSQL 等では読み取り専用ロールの DSN をここに置く**。設定するとプログラム SQL
    # （`sql_extract` と統計プロファイルの自アプリDB集計）はこの DSN の別エンジンで、
    # 読み取り専用トランザクションの中で実行される。未設定のとき、ENVIRONMENT=production では
    # 実行しない（フェイルクローズ。読み取り専用トランザクションは `users.password_hash` の
    # ような読み取りを止めない）。production 以外ではアプリ本体のエンジンから別接続を取り、
    # 読み取り専用トランザクションで実行する。どちらも起動時と `kawasemi check_schema` で
    # 警告する（`kawasemi/core/readonly_db.py`）。
    readonly_database_url: str = Field(
        default="",
        description="プログラムSQL専用の読み取り専用DSN（PostgreSQL等。SQLiteでは不要）",
    )
    # プログラム SQL（PostgreSQL）の文単位のタイムアウト。読み取り専用トランザクションの
    # 先頭で `SET LOCAL statement_timeout` 相当を掛ける。0 で無効。
    readonly_statement_timeout_seconds: int = Field(
        default=60,
        ge=0,
        description="プログラムSQLのstatement_timeout（秒。PostgreSQL。0で無効）",
    )

    # リソース索引（indexer 出力。ステップ1役割2のembedding検索で参照。施設固有）
    resource_index_dir: str = Field(
        default="",
        description="リソース索引ディレクトリ（*_index.jsonl + embeddings.npy + build_info.json）",
    )
    # 索引生成・クエリ双方で使う embedding モデル（索引側と一致必須）
    embedding_model: str = Field(
        default="bge-m3",
        description="リソース索引のembeddingモデル名（Ollama）。索引生成時と一致させること",
    )
    # embedding だけ別の Ollama へ向ける（例: LLM は外部サーバ、bge-m3 は localhost）
    embedding_base_url: str = Field(
        default="",
        description="embedding用のOllamaサーバURL。空なら ollama_base_url を流用する",
    )
    # リソース索引検索の最小 cosine スコア（これ未満の候補はLLMへ渡さず足切り）
    resource_index_min_score: float = Field(
        default=0.5,
        description="データ所在AI案のembedding検索の足切り閾値（cosine）。埋め込みモデルと索引で分布が変わるので施設ごとに調整する。",
    )
    # 接続レジストリ（connection_id→接続詳細/scope。施設固有・gitignore対象）
    connections_file: str = Field(
        default="",
        description="接続レジストリ connections.yaml のパス（connection_id→kind/scope/接続詳細）",
    )
    # キー種別カタログ（レベル1：key_type→canonical format。施設固有・gitignore対象）
    key_types_file: str = Field(
        default="",
        description="キー種別カタログ key_types.yaml のパス（key_type→label/system/canonical）",
    )
    # 典拠の範囲（様式ごとに典拠にしてよい規約文書。施設固有・gitignore対象）
    reference_scopes_file: str = Field(
        default="",
        description=(
            "様式（Questionnaire.url）ごとに典拠にしてよい規約文書を列挙する YAML のパス。"
            "無ければ範囲を設けない（全文書が典拠候補）。あれば、載っていない様式には典拠を出さない"
        ),
    )
    # 所在の規則（検索で届かない所在の受け皿など。施設固有・gitignore対象）
    location_rules_file: str = Field(
        default="",
        description=(
            "データ所在の AI 案に施設が足す規則（location_rules.yaml）のパス。"
            "無ければ空の規則（受け皿を足さない）"
        ),
    )
    # 集計の対象項目（様式ごとの分母。様式・研究固有・gitignore対象）
    target_items_file: str = Field(
        default="",
        description=(
            "様式（Questionnaire.url）ごとに集計の分母に入れる項目番号を列挙する YAML"
            "（target_items.yaml）のパス。無ければ項目番号を持つ項目すべてが対象"
        ),
    )
    # モデルごとの options の例外（推論サーバとモデルの組み合わせ固有・gitignore対象）
    model_options_file: str = Field(
        default="",
        description=(
            "モデル名ごとに推論の options へ足す値（model_options.yaml）のパス。"
            "無ければどのモデルにも何も足さない"
        ),
    )
    # 被験者ID解決(resolver)。名寄せ値は KAWASEMI 非保持・非受領、外部解決のみ。
    #
    # **backend の切り替えは持たない**。以前は `KEY_RESOLVER_BACKEND` と
    # `KEY_RESOLVER_HTTP_*` 7項目で別プロセス HTTP resolver を選べたが、
    # 選ぶ設定が本番・デモ・CI のどこにも無く、実装は自分のテストからのみ呼ばれていた。
    # 名寄せ系を物理隔離する要求が出た時点で、その利用者と一緒に足し直す。
    key_resolver_anchor: str = Field(
        default="trial_subject_no",
        description="登録番号に対応する anchor key_type（FHIRでは system|登録番号 で Patient 検索）",
    )

    # ファイル出力設定
    questionnaire_output_dir: str = Field(default="", description="Questionnaire出力ディレクトリ")
    questionnaire_response_output_dir: str = Field(
        default="",
        description="QuestionnaireResponse出力ディレクトリ",
    )

    @model_validator(mode="after")
    def _fill_default_paths(self) -> "Settings":
        """空のパスを `site_dir` / `var_dir` から決める（明示した値はそのまま使う）。"""
        site, var = Path(self.site_dir), Path(self.var_dir)
        defaults = {
            "db_definition_dir": site / "db_definition",
            "connections_file": site / "connections.yaml",
            "key_types_file": site / "key_types.yaml",
            "reference_scopes_file": site / "reference_scopes.yaml",
            "location_rules_file": site / "location_rules.yaml",
            "target_items_file": site / "target_items.yaml",
            "model_options_file": site / "model_options.yaml",
            "resource_index_dir": var / "resource_index",
            "questionnaire_output_dir": var / "output" / "questionnaires",
            "questionnaire_response_output_dir": var / "output" / "questionnaire_responses",
        }
        for name, path in defaults.items():
            if not getattr(self, name):
                setattr(self, name, path.as_posix())
        return self

    @model_validator(mode="after")
    def reject_legacy_enable_ai(self) -> "Settings":
        """廃止済み ENABLE_AI が設定されていたら起動を拒否し、移行先を案内する。

        extra="ignore" のため放置すると黙って無視され「AIが有効のつもり」の
        構成事故になる。エイリアス展開はせず fail-fast とする（クリーンブレイク）。
        """
        legacy_in_environ = any(k.upper() == "ENABLE_AI" for k in os.environ)
        legacy_in_dotenv = False
        # env_file は単一の str|Path 設定だが型は Sequence|None も含むため絞り込む
        env_file = self.model_config.get("env_file")
        if not legacy_in_environ and isinstance(env_file, (str, Path)) and Path(env_file).exists():
            for line in Path(env_file).read_text(encoding="utf-8").splitlines():
                key = line.split("=", 1)[0].strip()
                if not line.lstrip().startswith("#") and key.upper() == "ENABLE_AI":
                    legacy_in_dotenv = True
                    break
        if legacy_in_environ or legacy_in_dotenv:
            raise ValueError(
                "ENABLE_AI は廃止されました。AI機能フラグは2つに分割されています: "
                "ENABLE_AI_DRAFTING（候補探索AI: 所在AI案・手順書生成・プラン作成）/ "
                "ENABLE_AI_EXTRACTION（自動構造化AI: 実行時の llm_extract / llm_judge）。"
                "旧来の ENABLE_AI=true 相当は両方を true に設定してください"
                "（詳細: docs/ai_flags_spec.md）"
            )
        return self

    @model_validator(mode="after")
    def forbid_debug_in_production(self) -> "Settings":
        """production では DEBUG / LOG_LEVEL=DEBUG を禁止する。

        debug=True は database.py の echo=settings.debug 経由で SQL バインドパラメータ
        （患者値=PHI）を、LOG_LEVEL=DEBUG は各所の debug ログを本番へ露出させる。
        構成事故を防ぐため起動を拒否する（fail-fast）。
        """
        if self.environment == "production":
            if self.debug:
                raise ValueError(
                    "production では DEBUG=true を使用できません"
                    "（SQLバインドパラメータ経由で PHI がログへ露出するため）"
                )
            if self.log_level == "DEBUG":
                raise ValueError("production では LOG_LEVEL=DEBUG を使用できません")
        return self

    # 入り口。使う人ごとに画面と権限の範囲を分ける。**コードと DB は分けない。**
    # 同じアプリを `ENTRY_ROLES` を変えて複数起動し、nginx で振り分ける（docs/reverse_proxy.md §2.5）。
    entry_name: str = Field(
        default="",
        description="この入り口の表示名（例: 設計）。空なら入り口の区別なし",
    )
    entry_roles: str = Field(
        default="",
        description=(
            "この入り口で使える役割（カンマ区切り。例: designer）。役割と入り口は1対1。"
            "空なら全役割（従来どおり）。役割名は kawasemi.models.role.RoleType.ALL に限る"
        ),
    )
    entry_urls: str = Field(
        default="",
        description=(
            "役割ごとの入り口の URL（`役割=URL` をカンマ区切り。例: collector=https://host/collect/,admin=https://host/admin/）。"
            "この入り口で使えない役割の画面へ誘導するときに使う。無ければ URL を出さない"
        ),
    )

    @field_validator("root_path", mode="before")
    @classmethod
    def normalize_root_path(cls, value: str) -> str:
        """サブパスを「先頭スラッシュあり・末尾スラッシュなし」へ正規化する。

        "kawasemi" / "/kawasemi" / "/kawasemi/" → "/kawasemi"、
        "" / "/" → ""（ルート直下）。
        """
        if not isinstance(value, str):
            return ""
        trimmed = value.strip().strip("/")
        return f"/{trimmed}" if trimmed else ""

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_sqlite_database_url(cls, value: str) -> str:
        """相対SQLiteパスをプロジェクトルート基準の絶対パスへ正規化する。

        インメモリ（`:memory:`）と URI 形式（`file:...`）はパスではないのでそのまま返す。
        """
        sqlite_prefix = "sqlite+aiosqlite:///"
        if not isinstance(value, str) or not value.startswith(sqlite_prefix):
            return value

        db_path = value[len(sqlite_prefix) :]
        if not db_path or db_path.startswith((":memory:", "file:")):
            return value

        path_obj = Path(db_path)
        if path_obj.is_absolute():
            return value

        resolved = (PROJECT_ROOT / path_obj).resolve().as_posix()
        return f"{sqlite_prefix}{resolved}"

    @property
    def trial_run_patient_id_set(self) -> frozenset[str]:
        """試行実行を許す患者IDの集合（`trial_run_patient_ids` を正規化したもの）。"""
        return frozenset(p.strip() for p in self.trial_run_patient_ids.split(",") if p.strip())

    @property
    def embedding_host(self) -> str:
        """embedding の接続先（`embedding_base_url` が空なら `ollama_base_url`）"""
        return self.embedding_base_url.strip() or self.ollama_base_url

    @property
    def allowed_hosts_list(self) -> list[str]:
        """CORS で許可するオリジンをリストで取得（`allow_origins` に渡す）"""
        return [host.strip() for host in self.allowed_hosts.split(",")]

    @property
    def questionnaire_output_dir_path(self) -> Path:
        """Questionnaire出力ディレクトリのPathオブジェクトを取得"""
        return Path(self.questionnaire_output_dir)

    @property
    def questionnaire_response_output_dir_path(self) -> Path:
        """QuestionnaireResponse出力ディレクトリのPathオブジェクトを取得"""
        return Path(self.questionnaire_response_output_dir)


# グローバル設定インスタンス（必須フィールドは環境変数から取得）
settings = Settings()  # type: ignore[call-arg]
