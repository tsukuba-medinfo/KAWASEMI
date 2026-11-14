"""AI入力支援モジュールのDBモデル定義"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kawasemi.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from kawasemi.models.questionnaire import Questionnaire
    from kawasemi.models.user import User


class SourceCategory:
    """ソース種別定数"""

    FHIR = "fhir"
    LOCAL_DB = "local_db"
    EXTERNAL_DB = "external_db"
    FILE = "file"
    LLM = "llm"
    MANUAL = "manual"
    # 「この施設にこのデータは存在しない」ことを人が明示した状態。手動入力（manual）とは
    # 意味が異なり、後段の取得手順は不要（has_content=False で手順書対象外になる）。
    NOT_AVAILABLE = "not_available"

    ALL = [FHIR, LOCAL_DB, EXTERNAL_DB, FILE, LLM, MANUAL, NOT_AVAILABLE]

    LABELS: dict[str, str] = {
        FHIR: "FHIRサーバ",
        LOCAL_DB: "ローカルDB",
        EXTERNAL_DB: "外部DB",
        FILE: "ファイル",
        LLM: "LLM補完",
        MANUAL: "手動入力",
        NOT_AVAILABLE: "該当データなし",
    }


class ExecutionStatus:
    """実行ステータス定数"""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class FailureCode:
    """取得失敗理由の構造化コード

    executor が step 実行の失敗・空振りを分類して evidence の step トレースと
    項目（link_id）単位に付与する。自由文 error と併記され、横断集計
    （項目別の自動充足率・失敗理由内訳）の一次データになる。

    schema_mismatch と not_found_for_patient の区別が最重要:
    前者は「所在同定の失敗」（想定した表・カラム・ファイルが実在しない）、
    後者は「所在は正しいが当該患者のデータが無い」。前者が
    「所在同定が困難な項目」を特定する一次データになる。

    unclassified はコード未付与（旧レコード）・分類不能の受け皿。集計側は
    failure_code を持たない既存 evidence をこの値として扱う。

    path_not_permitted は file/csv のパスがポリシーで拒否されたこと。
    **`unclassified` にも `schema_mismatch` にも寄せない。** 前者に入れると「分類不能」が
    設定ミスで膨らみ、**「所在同定が困難な項目」を特定するための指標が汚れる**。後者に寄せると
    「想定した表・カラム・ファイルが実在しない」（設計課題）と「接続ルート外を指した」
    （設定・プランの誤り）が同じコードになり、内訳から切り分けられなくなる。

    not_applicable は「患者の文脈上そもそも該当しない」。**取得の失敗ではない**
    ので、取得困難度の分母から除いて別掲する（`metrics._DENOMINATOR_EXCLUDED_CODES`）。
    needs_input との違いは「判定が動いたか」である。判定 op（`_JUDGMENT_OPS`）が実行され、
    **その判定を guard が指している**結果として取得系
    step が全て skip されたなら not_applicable（人の入力を待つ必要がない）、実行された step が
    皆無なら needs_input（自動取得が試みられていない）。

    answer_type_mismatch は「値は得られたが項目の回答型（date / dateTime / integer / decimal /
    boolean / choice）に合わない」こと。LLM の断り文（「不明（本文を貼って
    いただければ…）」）が date 項目の値として prefill され、valueString で保存された事故を
    受けて加えた。値は prefill せず（項目の value は None）、却下した値は evidence の
    `rejected_value` に残す。**分母からは除かない**——取得できなかったことに変わりはない。
    """

    SOURCE_UNAVAILABLE = "source_unavailable"  # 接続先ダウン・認証失敗・接続未解決
    KEY_RESOLUTION_FAILED = "key_resolution_failed"  # 患者キー解決不能（resolver 不通/未登録）
    NOT_FOUND_FOR_PATIENT = "not_found_for_patient"  # 所在は正しいが当該患者のデータが無い
    EXTRACTION_LOW_CONFIDENCE = "extraction_low_confidence"  # LLM 抽出が低信頼・空
    TIMEOUT = "timeout"  # タイムアウト
    SCHEMA_MISMATCH = (
        "schema_mismatch"  # 想定した表・カラム・ファイルが実在しない（所在同定の誤り）
    )
    NEEDS_INPUT = "needs_input"  # manual 項目・未確定 op のスキップ
    UNCLASSIFIED = "unclassified"  # 分類不能・コード未付与（旧レコード）
    NOT_APPLICABLE = "not_applicable"  # 患者の文脈上そもそも該当しない（分母から除く）
    PATH_NOT_PERMITTED = "path_not_permitted"  # 接続ルート外・絶対パス等のポリシー拒否
    ANSWER_TYPE_MISMATCH = "answer_type_mismatch"  # 値が項目の回答型に合わない

    # **末尾に追加する。** CSV のヘッダとセル（`routes.py` の取得困難度 CSV）とバッジ順が
    # この順序をそのまま使うので、途中に挿入すると既存 CSV との列の対応が黙って崩れる。
    ALL = [
        SOURCE_UNAVAILABLE,
        KEY_RESOLUTION_FAILED,
        NOT_FOUND_FOR_PATIENT,
        EXTRACTION_LOW_CONFIDENCE,
        TIMEOUT,
        SCHEMA_MISMATCH,
        NEEDS_INPUT,
        UNCLASSIFIED,
        NOT_APPLICABLE,
        PATH_NOT_PERMITTED,
        ANSWER_TYPE_MISMATCH,
    ]

    LABELS: dict[str, str] = {
        SOURCE_UNAVAILABLE: "接続先利用不可",
        KEY_RESOLUTION_FAILED: "患者キー解決失敗",
        NOT_FOUND_FOR_PATIENT: "当該患者データなし",
        EXTRACTION_LOW_CONFIDENCE: "抽出低信頼・空",
        TIMEOUT: "タイムアウト",
        SCHEMA_MISMATCH: "所在同定の誤り",
        NEEDS_INPUT: "手動入力・未確定",
        UNCLASSIFIED: "未分類",
        NOT_APPLICABLE: "該当なし（分母外）",
        PATH_NOT_PERMITTED: "パス不許可（設定の誤り）",
        ANSWER_TYPE_MISMATCH: "回答型に合わない値",
    }


class AnnotationStatus:
    """臨床的コンテキスト（ステップ1・役割1）の確認ステータス定数

    語彙は RunbookStatus / PlanStatus と同一（draft / confirmed）。設計ロールの
    4成果物すべてを「下書き → 人間確認 → confirmed 昇格」の同じワークフローに
    乗せるため、臨床的コンテキストにも同じ2値を持たせる。
    """

    DRAFT = "draft"
    CONFIRMED = "confirmed"

    ALL = [DRAFT, CONFIRMED]

    LABELS: dict[str, str] = {
        DRAFT: "未確認",
        CONFIRMED: "確認済み",
    }


class RunbookStatus:
    """手順書（ステップ2）の確認ステータス定数"""

    DRAFT = "draft"
    CONFIRMED = "confirmed"

    ALL = [DRAFT, CONFIRMED]

    LABELS: dict[str, str] = {
        DRAFT: "未確認",
        CONFIRMED: "確認済み",
    }


class PlanStatus:
    """実行計画（ステップ3の出力 / ステップ4の入力）の確認ステータス定数

    confirmed のみステップ4（autofill）で実行可能。program に needs_input が残る
    限り confirmed へ昇格できない（人間がSQL等を補完してから確認する）。
    """

    DRAFT = "draft"
    CONFIRMED = "confirmed"

    ALL = [DRAFT, CONFIRMED]

    LABELS: dict[str, str] = {
        DRAFT: "未確認",
        CONFIRMED: "確認済み",
    }


# 計画のステップに書ける SQL の長さの上限（文字数）。SQL の検査（`dsl.is_select_only_sql`）は
# 正規表現と字句解析で全文を読むので、無制限だと長い入力で処理時間が伸びる。生成・索引由来の
# SQL は数百〜数千文字で、これを超える正当な用途は無い。スキーマと検査の両方で同じ値を使う。
MAX_SQL_LENGTH = 20_000


class OpType:
    """DSLプログラムのプリミティブ命令（op）定数

    ステップ3が手順書から作成し、ステップ4の安全インタプリタが実行する。
    各 step は id を持ち、結果を実行コンテキストへ束縛、後続が "$id" で参照する。
    """

    SQL_EXTRACT = "sql_extract"  # 自アプリDBからSELECTで抽出
    # fhir_path は FHIRPath ではなく簡易ドット記法（executor.resolve_fhir_path）
    FHIR_EXTRACT = "fhir_extract"  # FHIRサーバ取得＋簡易ドット記法で値抽出
    EXTERNAL_DB_EXTRACT = "external_db_extract"  # 外部DB（connection_id経由）からSELECT抽出
    FILE_EXTRACT = "file_extract"  # patient_dataファイル抽出（txt/md/csv/json/xml/pdf、画像PDFはVision OCR縮退）
    CSV_EXTRACT = "csv_extract"  # 全患者横断CSVを患者ID列＋複合等値WHEREで決定的に抽出
    LLM_EXTRACT = "llm_extract"  # LLMで自由記述から値を抽出
    LLM_JUDGE = "llm_judge"  # LLMで真偽/分類を判定
    COMPARE = "compare"  # left cmp right → bool
    BRANCH = "branch"  # when/then/else → 値
    TRANSFORM = "transform"  # code_map / unit_conversion
    SELECT = "select"  # 複数候補(rows)から pick(first/last)で1件選ぶ（where/orderは非実行ヒント）
    AGGREGATE = "aggregate"  # 複数候補(rows)を集計してスカラ化

    ALL = [
        SQL_EXTRACT,
        FHIR_EXTRACT,
        EXTERNAL_DB_EXTRACT,
        FILE_EXTRACT,
        CSV_EXTRACT,
        LLM_EXTRACT,
        LLM_JUDGE,
        COMPARE,
        BRANCH,
        TRANSFORM,
        SELECT,
        AGGREGATE,
    ]

    LABELS: dict[str, str] = {
        SQL_EXTRACT: "SQL抽出",
        FHIR_EXTRACT: "FHIR抽出",
        EXTERNAL_DB_EXTRACT: "外部DB抽出",
        FILE_EXTRACT: "ファイル抽出",
        CSV_EXTRACT: "CSV抽出",
        LLM_EXTRACT: "LLM抽出",
        LLM_JUDGE: "LLM判定",
        COMPARE: "値比較",
        BRANCH: "条件分岐",
        TRANSFORM: "変換",
        SELECT: "選択",
        AGGREGATE: "集計",
    }


class AssessmentStage:
    """データ所在の確からしさの判定の4段階。

    順序尺度であり、判定開始後に段階の追加・意味変更をしない前提でコード定数として
    固定する（DB側で自由入力にすると集計が破綻する）。数値は段階の高さの順序を表す。
    """

    DEFINITE = 4  # 確実: その列・欄は値を持つように作られており、実際にも毎回埋まる
    LIKELY = 3  # 高い: たいていは取れる。ただし抜けや書き方の揺れがときどきある
    DOUBTFUL = 2  # 疑わしい: 候補はあるが、運用を見る限り取れないことが多そうである
    ABSENT = 1  # 不在: その施設ではこの情報を記録していない

    ALL = [DEFINITE, LIKELY, DOUBTFUL, ABSENT]

    LABELS: dict[int, str] = {
        DEFINITE: "確実",
        LIKELY: "高い",
        DOUBTFUL: "疑わしい",
        ABSENT: "不在",
    }

    # DB の CHECK 制約の名前。移行と ORM で**同じ名前**を使う
    # （名前が違うと、移行で作った制約を後の移行が落とせない）。
    CHECK_NAME = "ck_location_candidate_assessments_stage_vocabulary"

    @classmethod
    def is_valid(cls, stage: object) -> bool:
        """`stage` が語彙内か（`None` は「判定不能」なので許す）。

        **`int` 以外を拒否する。** この述語が受け取るのは保存 API の入力だけではなく、
        **ORM が DB から読んだ値**（集計・集約規則）でもある。CHECK 制約が無い環境で
        `UPDATE ... SET stage = 'True'` のように書かれた行は、ORM が **`str` のまま**返す
        （実測）——`str(latest.stage)` が集計キーになるので `"True"` という新しいキーができる。
        `int()` へ通してから比べると `"4"` のような文字列を語彙内に昇格させてしまうので、
        **型のまま拒否する**。

        **`in ALL` との差は2つある**（いずれも実測した）。

        1. **`None`**: `in ALL` は偽、`is_valid` は真。**この差はこの実装が依存している差**である
           （判定不能の行は `stage IS NULL`）。`is_valid` を `in ALL` へ戻すと、判定不能の項目が
           すべて 409 / `ValueError` に落ちる（実測: 振る舞いのテスト6本が赤くなる）
        2. **`bool` と、値の等しい非 int の数値**（`True` / `1.0` / `Decimal("1")` など）:
           `in ALL` は真、`is_valid` は偽。**この差はどの既知の経路からも到達しない**——保存 API は
           pydantic v2 が `stage: int | None` の `True` を `1` へ強制し（実測: `stage=true` は改修の
           前後どちらでも 200、集計キーは `"1"`）、INTEGER 親和性の列から ORM が `bool` や `float`
           を返す経路も無い。実測: **型検査だけを落とす変異は振る舞いのテストを1本も落とさない**

        したがって **`bool`／非 int 数値の枝は多重防御であって層ではない**（「効くのは ORM 側だけ」と
        書いていたのも測れない主張だった。到達する経路を示せないまま層として数えない）。
        **`int` 以外を型のまま拒否する**理由は上に書いたとおり——`int()` へ通すと `"4"` のような
        文字列を語彙内へ昇格させてしまう（`str` が返る経路は実在するが、それは `in ALL` でも
        弾かれるので、ここでも層ではない）。
        """
        if stage is None:
            return True
        if isinstance(stage, bool) or not isinstance(stage, int):
            return False
        return stage in cls.ALL

    @classmethod
    def row_is_in_vocabulary(cls, stage: object, is_undeterminable: object) -> bool:
        """判定1件の `(stage, is_undeterminable)` の**組**が語彙内か。

        **`stage` 単体では語彙を決められない。** `None` が語彙内なのは「判定不能」を意味する
        ときだけで、`is_undeterminable = 0` の行の `None` は**段階が付いていないだけの行**である。
        `is_valid` だけで判定していたときは、その行が `str(latest.stage)` を経て
        **`summary["manual"]["None"] = 1`** になった（実測）。語彙外の値が作る集計キー
        （`"0"` や `"None"`）のうち、**`"None"` の側が塞がっていなかった**。

        さらに恒等式 `assessed_items == Σitems_by_stage + undeterminable_items` も破れる
        （`_best_stage_per_item` はこの行を `staged` にも `undeterminable` にも入れない）。

        **排反を語彙の一部として扱う**（3層とも同じ規則を使う）。この排反はそれまで保存 API の
        1行（`create_location_assessment` の 400）だけが守っており、**「門番が API の1行だけ」
        という形がこの組み合わせに残っていた**。

        鏡像（`is_undeterminable = 1` かつ `stage` が非 NULL）もここで落ちる。新しい集計キーは
        生まれないが、**段階の値が黙って捨てられる**（`is_undeterminable` を先に見るため）。

        `is_undeterminable` が `bool` / `int` でなければ**語彙外に倒す**（fail-closed）。
        **ただしこの枝はどの実測経路からも発火しない**——列は
        `Boolean, nullable=False` で、DB 直書きで `'abc'` や `2` が入っても **SQLAlchemy の
        `Boolean` が `True` へ変換して**この述語へ渡すので、届くのは `bool` だけである。
        `stage` 側の非 int 枝と同じく**多重防御であって層ではない**。
        """
        if not cls.is_valid(stage):
            return False
        if not isinstance(is_undeterminable, (bool, int)):
            return False
        return (stage is None) == bool(is_undeterminable)

    @classmethod
    def check_sql(cls) -> str:
        """CHECK 制約の SQL 式。**語彙は `ALL` が唯一の出所**（式に数値を直書きしない）。

        `row_is_in_vocabulary` と同じ規則を SQL で書く（**排反込み**）。`stage` 単体を見る式
        （`stage IS NULL OR stage IN (...)`）では `is_undeterminable = 0` の NULL 行が通り、
        集計に `"None"` というキーが生まれる。

        **`stage IS NOT NULL` を明示的に置く**。これが無いと `stage` が NULL で
        `is_undeterminable = 0` の行で式が **NULL へ潰れ**（`NOT 0 AND (NULL IN (...))` は NULL）、
        **SQL の CHECK 制約は NULL を「満たした」として通す**——塞ごうとした行がそのまま入る
        （実測。三値論理の穴で、`row_is_in_vocabulary` の Python 版とは挙動が分かれる）。
        """
        values = ", ".join(str(v) for v in cls.ALL)
        return (
            f"(is_undeterminable AND stage IS NULL) OR "
            f"(NOT is_undeterminable AND stage IS NOT NULL AND stage IN ({values}))"
        )


class VariantDependentNote:
    """様式の版や対象群によって所在が変わるか（該当する項目のみ回答）"""

    VARIABLE = "variable"  # 変わる
    INVARIANT = "invariant"  # 変わらない
    UNKNOWN = "unknown"  # 不明

    ALL = [VARIABLE, INVARIANT, UNKNOWN]

    LABELS: dict[str, str] = {
        VARIABLE: "変わる",
        INVARIANT: "変わらない",
        UNKNOWN: "不明",
    }


def compute_cand_key(candidate: dict[str, Any]) -> str:
    """所在候補1件を指す安定な参照キーを、候補の内容から決定的に導出する。

    candidates 配列に永続IDを持たせる代わりに、判定の対象を特定する識別子（source_category
    / location / unit_id / connection_id）のハッシュを使う。理由:

    - candidates は保存のたびに `_normalize_candidates` が生JSONから作り直す配列であり、
      フロントエンドはこの4フィールドしか往復させない。永続IDを足すには保存経路とUIの
      往復対応が必要になり、対応漏れがあると無関係な保存操作で判定が孤児化する
    - 内容が同じなら常に同じキーになるため、無関係な項目を保存し直しても既存候補の
      キーは変わらない。一方で候補の内容そのものが変われば別候補として扱われる。これは
      「判定は提示された特定の記載内容に対して行う」という
      「提示した所在」の考え方と整合する

    `llm_model` と `payload` はキーに含めない。前者は来歴、後者は付随情報であり、
    どちらも所在の同一性を左右しない。
    """
    identity = {
        "source_category": candidate.get("source_category"),
        "location": candidate.get("location"),
        "unit_id": candidate.get("unit_id"),
        "connection_id": candidate.get("connection_id"),
    }
    canonical = json.dumps(identity, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class ClinicalAnnotation(Base, TimestampMixin):
    """臨床的コンテキストアノテーション（施設非依存）

    ステップ1の役割1。Questionnaire item ごとの臨床ロジックを自由記述で保持する。
    治験実施計画書（プロトコル）由来の記述を取り込み、複数施設で標準化・共有できる。
    本テーブルが正であり、Questionnaire の FHIR Extension へ手動で書き戻し/読込する。

    citations は記述の典拠（規約文書の該当箇所）の配列で、各要素は
    ClinicalAnnotationCitation（schemas.py）相当:
        {unit_id, payload, llm_model}
    - unit_id: 索引側のアドレス。reference 文書なら
      `file:{パス}#p{ページ}:{開始文字}-{終了文字}`（indexer の `file_indexer`）
    - payload: 索引由来の機械可読情報（label / snippet / path / page 等）。手入力時は
      抜粋のみを持つことがある
    - llm_model: 典拠を差し込んだ候補探索AI由来の来歴（手入力時は null）。人間が
      記述を編集しても由来の事実は変わらないため保持する

    status / llm_model / confirmed_by_id / confirmed_at は InputRunbook・InputPlan と
    同形の来歴で、「下書き → 人間確認 → confirmed 昇格」の確定ワークフローに乗せる。
    """

    __tablename__ = "clinical_annotations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    link_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Questionnaire item の linkId",
    )

    item_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="表示用テキスト",
    )

    annotation: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="臨床ロジックの自由記述（起点・時間窓・優先順・条件分岐など）",
    )

    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="記述の典拠（規約文書の該当箇所）の配列（unit_id/payload/llm_model）",
    )

    # **この項目の下書きが矛盾した応答由来か**。保存時に設計ロールの来歴
    # （`DesignLlmCall.duplicated_link_ids`）から引いて立てる——**クライアントの申告には
    # 依存しない**（自己申告の `origin` は信用できない）。集計では分子からも
    # 「規約文書を引いても意味が決まらなかった項目」からも外して**別掲**する（分子の定義そのものは変えない）。
    duplicated_response: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
        comment="矛盾した応答（同じ項目に複数エントリ）由来の下書きか（別掲用）",
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AnnotationStatus.DRAFT,
        # alembic e1f2a3b4c5d6 と揃える（既存DBと create_all のスキーマ差を作らない）
        server_default=AnnotationStatus.DRAFT,
        comment="draft（下書き・未確認） / confirmed（人が確認済み）",
    )

    llm_model: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="臨床記述・典拠の下書きに使用したLLMモデル名（手入力時は null）",
    )

    # **「探したが規約文書に該当箇所が無かった」を人が書き残す欄**。
    #
    # 記述が空・典拠0件・status=draft という状態は、**「まだ書いていない」項目と
    # 「該当箇所が無いので空欄にした」項目で完全に同じ**だった。`save_clinical` は記述の
    # 空な項目を confirmed へ昇格させない（確認する内容が無いため）ので、確定ワークフロー
    # でも区別が付かない。この欄が埋まっていることが「人が探して、無いと判断した」記録になる。
    #
    # **AI が埋める欄ではない。** LLM 経路で典拠が付かなかった項目を
    # 「規約文書を引いても意味が決まらなかった項目」として報告してはいけない。三状態
    # （`DraftState`）が畳み込む「候補に無い / 候補0件 / 矛盾した応答」は人の判断ではない。
    #
    # **評価用の欄である**。実際の臨床試験では、項目の定義は
    # 必ずどこかの規約文書に存在する——存在しなければ、そもそもデータを正しく取得できない。
    # つまり「探したが該当箇所が無い」は**規約文書そのものの不備を示す観測値**
    # であって、運用機能ではない。**この前提が変わらない限り、機能を足さない。**
    # 特に「AI に埋めさせて件数を数える」は上で禁じた形そのものである。
    #
    # **記入者と時刻は持たない**。人の判断の記録でありながら `confirmed_by_id` /
    # `confirmed_at` に相当する帰属を持たないのは、**いまこの欄を書くのが1人**であり、
    # 誰が書いたかを行の外の記録で特定できるためである。
    # 「帰属が要らない」のではない——**書く人が増えたらここに帰属が要る。**
    # 他の記録との前後関係も行からは読めないので、順序は行の外の来歴が担保する。
    #
    # 配布物へは出ない（`file_export` が読むのは `{link_id: annotation テキスト}` だけで、
    # clinical-annotation Extension にこの列は載らない）。
    no_citation_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="典拠が見つからなかった理由（人が記録する。AIは書き込まない。評価用）",
    )

    # 確認操作の帰属（ALCOA+ 帰属性）。status=confirmed 昇格時に記録し、
    # draft へ戻る場合はクリアする。InputRunbook / InputPlan と同形。
    confirmed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="確認（confirmed 昇格）操作者",
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="確認日時（UTC）"
    )

    confirmed_by: Mapped[User | None] = relationship("User", foreign_keys=[confirmed_by_id])

    questionnaire: Mapped[Questionnaire] = relationship("Questionnaire")

    __table_args__ = (
        UniqueConstraint(
            "questionnaire_id",
            "link_id",
            name="uq_clinical_annotation_qn_link",
        ),
    )

    @property
    def citation_list(self) -> list[dict[str, Any]]:
        """常にリストを返す（None/未設定を [] に正規化）。"""
        return self.citations or []

    @property
    def is_confirmed(self) -> bool:
        """人間確認済み（confirmed 昇格済み）か。"""
        return self.status == AnnotationStatus.CONFIRMED

    @property
    def citation_llm_models(self) -> list[str]:
        """典拠に含まれるAI由来モデル名の一覧（確認書の開示欄用、重複除去・整列）。"""
        return sorted(
            {
                str(c["llm_model"])
                for c in self.citation_list
                if isinstance(c, dict) and c.get("llm_model")
            }
        )


class SystemLocation(Base, TimestampMixin):
    """データ所在情報（施設固有）

    ステップ1の役割2。データが物理的にどこにあるか（接続先・表・カラム）を保持する。
    施設ごとに異なるため Questionnaire の Extension には含めない。
    外部DB接続の実行実装は後回しで、今は記述・確認のみ。

    1項目の値は複数のソース候補を持ちうる（例: FHIR優先・無ければ院内DB）。所在は
    「どこにあるか」の候補集合とし、値の演算・選択・分岐はステップ3/4のDSLが担う。
    candidates は優先順の配列で、各要素は SystemLocationCandidate（schemas.py）相当:
        {source_category, location, unit_id, connection_id, payload, llm_model}
    - source_category: fhir/local_db/external_db/llm/manual
    - location: 接続先・表・カラムの人間可読記述
    - unit_id/connection_id/payload: リソース索引由来の機械可読情報（ステップ3が
      op引数を決定的に導出するための素材。手入力時は null）。ステップ3は
      routes._compile_inputs → compiler で候補配列ごと受け取り、
      resource_index.derive.derive_op_args で表・カラム・クエリ・パスを確定する
    - llm_model: 候補探索AI（AI案）由来の来歴。生成モデル名（手入力時は null）。
      確認書の「設計過程におけるAI利用」開示欄で使う
    """

    __tablename__ = "system_locations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    link_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Questionnaire item の linkId",
    )

    item_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="表示用テキスト",
    )

    candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="優先順のデータ所在候補の配列（source_category/location/unit_id/connection_id/payload）",
    )

    # **AI が提示した候補の全件**。`candidates` が人の決着であるのに対し、
    # こちらは決着の手前の**提示の記録**である。並びは画面へ出したときの提示順で、各要素は
    # `SystemLocationDraftCandidate` 相当（`score` / `stars` / `recommended` を含む）。
    #
    # **`candidates` へ相乗りさせない。** `candidates` はステップ3のコンパイラが読んで
    # 取得計画を組み立てる配列であり（`compiler._MAX_ALT_CANDIDATES`）、提示8件をそのまま
    # 入れると**選ばれなかった7件がフォールバック取得計画に化ける**。上限 3
    # （`MAX_LOCATION_CANDIDATES`）はその読まれ方から決まっているので、上限を広げても
    # 解決しない——広げるべきは提示の記録であって取得計画ではない。
    #
    # AI 提案の適合率と、AI が提案しなかった所在の数は、どちらも「AI が提案した候補」と
    # 「人が選んだもの」の関係を見る指標なので、
    # この列が分母になる。人が選び直しても提示の事実は変わらないため上書きしない
    # （AI 案を出し直したときだけ入れ替わる）。
    proposed_candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
        comment="AI が提示した候補の全件（提示順。人の決着は candidates 側）",
    )

    # 確認操作の帰属（ALCOA+ 帰属性）。ClinicalAnnotation / InputRunbook /
    # InputPlan と同形で、所在の側にだけ無かった非対称を埋める。
    #
    # **`is_resolved`（候補が選ばれている）と `is_confirmed`（人が確認した）は別物である。**
    # AI 推奨は画面で初期選択されるので、人が一度も触っていない項目でも `is_resolved` は
    # 真になる。この2つを混ぜると「人が見た項目」と「AI 推奨のまま素通りした項目」が
    # 区別できない。
    #
    # 確認は**項目単位**に記録する（`save_system` の `confirm` は request に含まれる
    # link_id だけを対象にする）。一括保存しか無いと `confirmed_at` が揃い、
    # 1項目あたりの工数が後から復元できない。
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AnnotationStatus.DRAFT,
        # alembic 85159bbd9a3e と揃える（既存DBと create_all のスキーマ差を作らない）
        server_default=AnnotationStatus.DRAFT,
        comment="draft（AI 案のまま・未確認） / confirmed（人が確認済み）",
    )

    confirmed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="確認（confirmed 昇格）操作者",
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="確認日時（UTC）。項目単位に記録するので工数の復元に使える",
    )

    confirmed_by: Mapped[User | None] = relationship("User", foreign_keys=[confirmed_by_id])

    # 職務分離: 所在の確からしさの判定は「設計成果物の作成者」と別人が行ったかを
    # 検出できる必要がある。ClinicalAnnotation の confirmed_by_id と異なり、
    # SystemLocation には確認ワークフローが無く帰属を持つ場所が無かったため追加した。
    # （確認ワークフローそのものは後から入ったが、この列は**最後に保存した人**であって
    # 確認者ではない。判定の職務分離は「その所在を書いた人」を見るのでこちらを使う。）
    # save_system で保存のたびに更新する（最後にこの場所を編集した人）。
    # 本カラム導入前に保存されたまま以後保存し直されていない行は null のままであり、
    # その間に記録される判定は実際の保存者に関わらず is_self_assessment=False になる
    # （導入前の行を職務分離違反として遡って検出することはできない）。
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="最後に保存した利用者（職務分離の判定に使う）",
    )

    updated_by: Mapped[User | None] = relationship("User", foreign_keys=[updated_by_id])

    questionnaire: Mapped[Questionnaire] = relationship("Questionnaire")

    __table_args__ = (
        UniqueConstraint(
            "questionnaire_id",
            "link_id",
            name="uq_system_location_qn_link",
        ),
    )

    @property
    def proposed_candidate_list(self) -> list[dict[str, Any]]:
        """常にリストを返す（None/未設定を [] に正規化）。"""
        return self.proposed_candidates or []

    @property
    def is_confirmed(self) -> bool:
        """人が確認済み（confirmed 昇格済み）か。`is_resolved` とは別物である。"""
        return self.status == AnnotationStatus.CONFIRMED

    # --- 単一候補を前提とする既存消費者向けの便宜プロパティ -----------------
    # 表示（設計レビュー表）・手順書生成（ステップ2は人間可読の所在で足る）・進捗判定は
    # 「主たる所在」を1つ読めば足りる。primary = candidates[0]（優先順の先頭）。
    # 一方でステップ3（DSLコンパイル）は候補全体を使う（多ソースのプログラムを step 単位で
    # 索引ユニットへ対応づけるため）。そちらは candidate_list を参照すること。

    @property
    def candidate_list(self) -> list[dict[str, Any]]:
        """常にリストを返す（None/未設定を [] に正規化）。"""
        return self.candidates or []

    @property
    def primary(self) -> dict[str, Any] | None:
        cands = self.candidate_list
        return cands[0] if cands else None

    @property
    def primary_source_category(self) -> str:
        p = self.primary
        return (p or {}).get("source_category") or SourceCategory.MANUAL

    @property
    def primary_location(self) -> str | None:
        p = self.primary
        return (p or {}).get("location") if p else None

    @property
    def has_content(self) -> bool:
        """所在として意味のある内容があるか（手順書設計対象の判定に使う）。

        「該当データなし（not_available）」は人が決着させた状態だが取得対象ではない
        ため、内容ありとは見なさない（手順書・プランを生成しない）。
        """
        for c in self.candidate_list:
            cat = c.get("source_category")
            if cat == SourceCategory.NOT_AVAILABLE:
                continue  # 取得対象外（手順書を作らない）
            if (c.get("location") or "").strip():
                return True
            if cat and cat != SourceCategory.MANUAL:
                return True
        return False

    @property
    def is_resolved(self) -> bool:
        """役割2として人が決着させたか（候補選択・手動入力・該当データなしの明示）。

        candidates が空 = 未解決（未選択）。後段（手順書生成）はこれが残る間は進めない。
        has_content と異なり、not_available も「解決済み」に含める点に注意。
        """
        return bool(self.candidate_list)


class LocationCandidateAssessment(Base, TimestampMixin):
    """データ所在の確からしさの判定1件

    SystemLocation の候補（candidates[] の1要素）を対象に、判定者が rubric の4段階
    （AssessmentStage）で担保の見込みを判定した記録。1候補に対して複数回の判定
    （1回目・再判定）を持てる（再現性の測定に使う）。追記のみで、既存行は更新・削除
    しない（判定記録そのものがALCOA+の対象であり、訂正は新しい round として残す）。

    候補は `cand_key`（compute_cand_key、候補内容のハッシュ）で参照する。
    `candidate_snapshot` に判定時点の候補内容をそのまま複製する。これは
    「提示した所在」の記録であり、後から候補が編集されても判定記録は
    判定時点の内容を保持する（再判定できる形での出力に必須）。

    候補の内容（source_category/location/unit_id/connection_id）そのものが変わると
    cand_key も変わるため、旧キーに紐づく判定履歴は新しい候補からは別系列として
    扱われる（round_number は引き継がれない）。これは意図的な設計で、判定は
    「提示された特定の記載内容」に対して行うものであり、候補が変われば同一の
    判定対象ではなくなるという考え方に基づく。旧履歴は system_location_id 経由で
    引き続き参照できる（孤児化ではなく、別候補として保持される）。

    is_self_assessment は判定者（assessed_by）と、その時点の SystemLocation の
    最終編集者（SystemLocation.updated_by）が同一人物かを判定時に固定した値。
    後から SystemLocation が別の人に編集されても本記録の値は変わらない
    （判定時点の職務分離状況を記録するものであるため）。
    """

    __tablename__ = "location_candidate_assessments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    system_location_id: Mapped[int] = mapped_column(
        ForeignKey("system_locations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    cand_key: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="判定対象の候補を指す参照キー（compute_cand_key の出力）",
    )

    candidate_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        comment="判定時点の候補内容の複製（「提示した所在」）",
    )

    round_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="同一候補への判定回次（1=1回目、2以降=再判定）",
    )

    stage: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="AssessmentStage の値。判定不能の場合は null",
    )

    is_undeterminable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="段階を付けず「判定不能」と回答された（推測で段階を付けない）",
    )

    rationale: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="判断の根拠（自由記述、判定不能の場合も理由を残す）",
    )

    unreachable_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="到達不能の理由（該当する場合のみ。手書きスキャン・他院紙文書・非標準記載など）",
    )

    variant_dependent_note: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="様式の版や対象群によって所在が変わるか（VariantDependentNote）",
    )

    materials: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="判定材料のスナップショット（スキーマ定義・データディクショナリ・統計プロファイル等）",
    )

    is_self_assessment: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="判定時点で判定者=SystemLocation.updated_by だったか（職務分離の検出用）",
    )

    assessed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="判定者",
    )

    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="判定日時（UTC）",
    )

    system_location: Mapped[SystemLocation] = relationship("SystemLocation")
    assessed_by: Mapped[User | None] = relationship("User", foreign_keys=[assessed_by_id])

    __table_args__ = (
        UniqueConstraint(
            "system_location_id",
            "cand_key",
            "round_number",
            name="uq_location_candidate_assessment_round",
        ),
        # **段階の語彙を DB でも守る**。以前は保存ルートの
        # `if payload.stage not in AssessmentStage.ALL: → 400` という**1行だけ**が門番で、
        # DB 直書き・移行スクリプト・将来の別エンドポイントは素通りした。集計は
        # `str(latest.stage)` を集計キーにするので、**語彙外の値は `"0"` や `"None"` という
        # 新しいキーを作る**——所在の確からしさの段階の目盛りが黙って増える。
        # 式は `AssessmentStage.check_sql()` が組む（語彙の出所を1つにする）。
        # **`(stage, is_undeterminable)` の組で見る**。`stage` 単体の
        # 式では `is_undeterminable = 0` の NULL 行が通り、`"None"` というキーがそのまま残った。
        CheckConstraint(AssessmentStage.check_sql(), name=AssessmentStage.CHECK_NAME),
    )

    @property
    def stage_label(self) -> str | None:
        if self.stage is None:
            return None
        return AssessmentStage.LABELS.get(self.stage)


class InputPlan(Base, TimestampMixin):
    """ステップ3の出力 / ステップ4の入力: 項目ごとの実行可能DSLプログラム

    確認済み手順書（InputRunbook）を Ollama でコンパイルして生成し（ステップ3）、
    ステップ4の安全インタプリタが実行する。program は限定プリミティブ（op）の
    線形SSA＋branch で表現され、任意コード実行を含まない。

    program（JSON）の構造:
        {
            "params": [                   # プログラム引数の宣言（任意）
                {
                    "name": str,          # 参照名（:name バインド / {{name}} 展開）
                    "kind": str,          # "value"（既定）| "identifier"（SQL識別子）
                    "value": Any|None,    # リテラル、または "$id"（前段 step の出力）
                    "allowed": [str]|None,# 許容値の許可リスト（identifier と "$id" 束縛では必須）
                    "description": str|None
                },
                ...
            ],
            "steps": [
                {
                    "op": str,            # OpType のいずれか
                    "id": str,            # 結果を束縛する変数名（後続が "$id" で参照）
                    ...op固有の引数...      # sql / fhir_query / prompt / left,cmp,right など
                    "needs_input": bool,  # 情報不足で人間補完が必要なら true（任意）
                    "reasoning": str|None # コンパイラの判断理由・不足情報（任意）
                },
                ...
            ],
            "return": "$id"               # 最終的に項目の値として採用する step の id 参照
        }

    params は「まず区分値を取得し、それを使って参照表名や FHIR の code= を組み立てる」
    段階的な取得を可能にする。差し込み先（スロット）ごとに実行時サニタイズが異なる:

        - SQL の ``:name``        → ドライバのバインド値（文字列連結しない）
        - SQL の ``{{name}}``     → 表名・カラム名のテキスト展開。kind=identifier 限定で
                                    厳格な識別子字種＋allowed を通し、展開後の SQL を
                                    再度 SELECT 限定検査する
        - fhir_query の ``{{name}}`` → 字種制限＋URLエンコード（クエリ改変を遮断）
        - path_pattern / path の ``{{name}}`` → パス構成要素として検査（区切り文字・
                                    上位参照を不許可）＋ root 配下検査
        - prompt の ``{{name}}``  → 外部データ由来の値はデータデリミタで囲む

    patient_id / study_id は宣言不要の組み込みパラメータ（再宣言不可）。
    study_id はバッチ実行ではジョブの値、単発・逐次の実行では要求本文の値（任意）が
    供給される（空のまま参照すると step が失敗）。

    status は draft/confirmed。program 内に needs_input=true の step が残る限り
    confirmed へ昇格できず、ステップ4では confirmed のみ実行可能。
    """

    __tablename__ = "ai_input_plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    link_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    item_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="表示用テキスト",
    )

    program: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="DSLプログラム {steps:[...], return:'$id'}",
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=PlanStatus.DRAFT,
        comment="draft（未確認・要レビュー） / confirmed（人が確認済み・実行可）",
    )

    llm_model: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="コンパイルに使用したLLMモデル名",
    )

    # 確認操作の帰属（ALCOA+ 帰属性）。status=confirmed 昇格時に記録し、
    # draft へ戻る場合はクリアする。QuestionnaireResponse.verified_by_id と同形。
    confirmed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="確認（confirmed 昇格）操作者",
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="確認日時（UTC）"
    )

    confirmed_by: Mapped[User | None] = relationship("User", foreign_keys=[confirmed_by_id])

    questionnaire: Mapped[Questionnaire] = relationship("Questionnaire")

    __table_args__ = (
        UniqueConstraint(
            "questionnaire_id",
            "link_id",
            name="uq_ai_input_plan_qn_link",
        ),
    )


class InputRunbook(Base, TimestampMixin):
    """ステップ2の出力: 項目ごとの「データ取得手順書（runbook）」

    2役割入力（臨床的コンテキスト＋データ所在）を突き合わせ、人間が読める
    順序付きステップのリストとして「どうデータを取得するか」を文書化する。
    各ステップは自由文中心だが action_type の軽いタグを持ち、将来ステップ3が
    確認済み(confirmed)手順書を InputPlan へコンパイルする際の手掛かりにする。

    steps の各要素（JSON）:
        {
            "order": int,            # 1始まりの表示順
            "title": str,            # ステップ見出し
            "description": str,      # 手順の自由文
            "action_type": str|None, # 種別タグ（schemas.RUNBOOK_ACTION_TYPES が正本）
            "data_hint": str|None,   # 参照データ源・クエリ例などの補足（任意）
        }
    """

    __tablename__ = "ai_input_runbooks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    link_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Questionnaire item の linkId",
    )

    item_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="表示用テキスト",
    )

    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="順序付き手順ステップの配列（order/title/description/action_type/data_hint）",
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RunbookStatus.DRAFT,
        comment="draft（生成直後・未確認） / confirmed（人が確認済み）",
    )

    llm_model: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="手順書生成に使用したLLMモデル名",
    )

    # 語彙外として `None` へ落とした種別タグの来歴。
    #
    # **保存要求が何を送ってきたかは、保存済みの steps には残らない**（検証が `action_type`
    # を `None` にするため）。弾かずに落とすのは ALCOA+ が要求するのが拒否ではなく可視性
    # だからで、そのぶん**落としたことの記録**が要る。
    #
    # 記録先をここにしたのは、これが**人の保存**であって LLM 呼び出しではないからである
    # （`ai_input_design_llm_calls` の行は LLM 呼び出しの記録で、role / model / options を
    # 持つ）。画面には出さず来歴に残す。
    #
    # **落ちたタグが無い保存では `None` にする**（空配列を残さない）。
    # 「語彙内だけの保存では何も残らない」ので、`null` が「この保存では落ちていない」を
    # 意味する。保存のたびに書き換えるので、前の保存の記録が残り続けることはない。
    dropped_action_types: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON,
        nullable=True,
        comment=(
            "語彙外として None へ落とした action_type の記録。"
            "[{order, given}] の配列。落ちたタグが無い保存では null"
        ),
    )

    # 確認操作の帰属（ALCOA+ 帰属性）。status=confirmed 昇格時に記録し、
    # draft へ戻る場合はクリアする。QuestionnaireResponse.verified_by_id と同形。
    confirmed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="確認（confirmed 昇格）操作者",
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="確認日時（UTC）"
    )

    confirmed_by: Mapped[User | None] = relationship("User", foreign_keys=[confirmed_by_id])

    questionnaire: Mapped[Questionnaire] = relationship("Questionnaire")

    __table_args__ = (
        UniqueConstraint(
            "questionnaire_id",
            "link_id",
            name="uq_ai_input_runbook_qn_link",
        ),
    )


class InputExecution(Base, TimestampMixin):
    """ステップ4（旧ステップ3）の実行ログ"""

    __tablename__ = "ai_input_executions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    patient_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="実行対象の患者ID",
    )

    subject_id: Mapped[int | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="被験者（Hub）。patient_id 文字列からの移行先。当面 nullable",
    )

    executed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # どのバッチジョブが生成したか（単発の autofill 実行では null）。集計・来歴の紐付け用。
    batch_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_input_batch_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="バッチ実行で生成された場合のジョブID（単発実行は null）",
    )

    prefill_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="{link_id: value} 形式の補完結果",
    )

    evidence: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment=(
            "{link_id: {trace:[...step単位（skipped? guard? 込み）...], "
            "value, error?, failure_code?}} 形式の根拠"
        ),
    )

    # 実行時に使用した DSL プログラムのスナップショット（各 step の reasoning 込み）。
    # プランは版管理が無く、実行後に編集・再確認で上書きされ得るため、実行記録がどの版の
    # プログラム・どの設計根拠で動いたかを固定保存し、後日の監査で実行ロジックを
    # 確定的に復元できるようにする（CSV / データインテグリティ: Original・Consistent）。
    # 形式: {link_id: {"program": {...}, "plan_id": int|null, "plan_updated_at": iso8601|null,
    #                  "confirmed_by_id": int|null, "confirmed_at": iso8601|null}}
    # confirmed_by_id / confirmed_at は後から加えたキーで、旧記録には無い（JSON 列なので
    # 移行は不要）。
    program_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="実行時使用の {link_id: {program, plan_id, plan_updated_at}} スナップショット",
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ExecutionStatus.SUCCESS,
        comment="success / partial / failed",
    )

    error_summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="エラー概要（失敗時）",
    )

    questionnaire: Mapped[Questionnaire] = relationship("Questionnaire")
    executed_by: Mapped[User | None] = relationship("User")


class InputTrialRun(Base, TimestampMixin):
    """設計ロールによる autofill 試行（trial）の監査ログ。

    autofill は実行コンテキストで区別する。設計クラスタ
    （臨床定義責任者／取得計画設計者／データ所在管理者）が「ちゃんと撮れているか」を
    検証する試行実行は、結果（prefill_data / evidence）を永続しない揮発プレビュー
    とし、本テーブルには「誰がいつどの患者で試したか」のメタ情報のみを残す
    （PHI を蓄積しない）。本番実行（収集者）は InputExecution に永続する。
    """

    __tablename__ = "ai_input_trial_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    patient_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="試行対象の患者ID（メタのみ。取得値そのものは保存しない）",
    )

    executed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    acted_as: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="試行時のアクティブ役割（designer。役割統合より前の行は collection_lead / plan_designer / data_manager）",
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ExecutionStatus.SUCCESS,
        comment="success / partial / failed",
    )

    filled_count: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
        comment="補完できた項目数（PHIを含まない件数のみ）",
    )

    questionnaire: Mapped[Questionnaire] = relationship("Questionnaire")
    executed_by: Mapped[User | None] = relationship("User")


class DesignRole:
    """設計ロールの LLM 呼び出しの区分。

    来歴を保存できるのはここに列挙した4段だけである。実行ロール（llm_extract /
    llm_judge）は患者データを読むので、来歴は evidence trace 側に閉じる。
    """

    CLINICAL = "clinical"  # ステップ1(役割1): 規約文書を典拠とする臨床的意味
    SYSTEM = "system"  # ステップ1(役割2): データ所在のAI案
    RUNBOOK = "runbook"  # ステップ2: 取得手順書
    PLAN = "plan"  # ステップ3: 取得プラン（DSL）

    ALL = frozenset({CLINICAL, SYSTEM, RUNBOOK, PLAN})

    LABELS: dict[str, str] = {
        CLINICAL: "臨床的意味",
        SYSTEM: "データ所在",
        RUNBOOK: "取得手順書",
        PLAN: "取得プラン",
    }


class DesignLlmCall(Base, TimestampMixin):
    """設計ロールの LLM 呼び出し1回の来歴。

    実行ロールが evidence trace に残している `llm{model, options, prompt, raw_output}`
    と同じ粒度で、プロンプト全文と生応答を保存する。「LLM が何を読んでその記述に
    したか」を後から確認できるようにするため。

    **患者データを保存しないことが本テーブルの前提である。** 設計ロールの入力は
    規約文書（公開文書）・索引のスキーマ定義・項目名・確認済み成果物であって、
    患者個票を含まない。論証と、前提が崩れる経路に対するガードは
    `docs/design_llm_provenance.md` に記録してある。

    実行ロール（llm_extract / llm_judge）の来歴は本テーブルへ入れない。`role` は
    DesignRole.ALL に限り、それ以外は保存時に拒否する（`save_design_llm_calls`）。
    """

    __tablename__ = "ai_input_design_llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="設計ロールの区分（clinical / system / runbook / plan）",
    )

    label: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="呼び出しの処理名（ログ・例外と同じラベル。例「手順書生成」）",
    )

    model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="生成に使ったモデル名",
    )

    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="ollama",
        comment=("推論プロバイダ（ollama 等）。モデル名だけでは実装が特定できないため別に持つ"),
    )

    options: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="生成 options（temperature / num_ctx 等）。再現性の記録",
    )

    system_prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="システムプロンプト全文",
    )

    user_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="ユーザメッセージ全文（項目名・所在記述・規約文書の抜粋など）",
    )

    raw_output: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="モデルの生応答（パース前）。失敗時も残す",
    )

    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="失敗した場合の理由（成功時は null）",
    )

    chunk_index: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
        comment="分割呼び出しの何回目か（chat_json_chunked）",
    )

    item_count: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
        comment="このチャンクで渡した項目数",
    )

    truncated: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
        comment="保存上限で切り詰めたか（切り詰めた事実を残す）",
    )

    content_sources: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment=(
            "プロンプトへ**原資料の中身**を入れた索引ユニットの出所（'kind:file_role' の"
            "一覧）。空なら中身を読んでいない（項目名・所在記述・確認済み成果物のみ）。"
            "kind=file は char_range を持つ中身つきユニットだけが対象で、所在宣言"
            "（path_pattern のみ）は含めない。中身つきファイルは file_role=reference に"
            "限る（design_provenance のガード2）"
        ),
    )

    # **検索に使った索引の同一性**。索引は版管理外（`.gitignore`）なので、
    # 差し替えは diff にも現れない。典拠の付与率は永続化された
    # `clinical_annotations.citations` から計算するため、**下書きを作った時点の索引と
    # 測定した時点の索引が別物でも値は出てしまう**。索引が実際に使われた瞬間に作られる
    # 記録（この行）へ残すことで、**典拠 → その典拠を作った呼び出し → 索引の同一性** を
    # 後から辿れるようにする。「指標を記録に残すときは build_info.json を併記する」という
    # **人への指示が、機械が担保する事実になる**。
    index_build_info: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="検索に使った索引の build_info（索引を読まないロールでは null）",
    )

    # **同じ項目に複数のエントリが返った link_id**。「矛盾した応答は分子からも
    # 「規約文書を引いても意味が決まらなかった項目」からも外して別掲する」と決めたが、その事実が**どこにも永続化
    # されていなかった**（プロセス内の戻り値・HTTP 応答・ログの3つだけ）。**サーバ側の
    # 真実**として、下書きを作ったその場（応答をマージした直後）に記録する。
    # **空配列と null は意味が違う**（空＝重複が無かった、null＝記録していない）。
    duplicated_link_ids: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="同じ項目に複数のエントリが返った link_id（空なら重複なし。null は未記録）",
    )

    # **使ったモデルのダイジェスト**。`model` のタグだけでは実装が特定できない
    # ——`ollama pull` で上流が更新されても、ローカルで同名タグを作り直しても、記録は同じ
    # 文字列のままである（「`.env` を書き換えるだけでモデルが変わる」問題の残り）。ここが入ることで「同じタグで中身が違うモデルを使った2回の実行」を記録だけで
    # 区別できる。**null の意味は2つあるので理由を隣の列に持つ。**
    model_digest: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="モデルのダイジェスト（取れなかった場合は null。理由は model_digest_unavailable）",
    )

    # **ダイジェストが取れなかった理由**。**空と null は意味が違う**——
    # 理由が入っていれば「引いたが取れなかった」、両方 null なら「宣言していない」
    # （ダイジェストを記録していなかった窓の記録、またはこの列を書かないロール）。
    # フェイルクローズにしない理由は `llm_provider.model_digest_of` にある（来歴の記録が
    # 生成を壊してはならない）。
    model_digest_unavailable: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="ダイジェストが取れなかった理由（取れた場合は null。両方 null は未宣言）",
    )

    # **thinking の設定と中身**。`think` は渡した値、`thinking` はモデルが
    # 返した推論本文である。**両方要る**——`think=True` でもモデルが何も考えないことは
    # ありうるので、`thinking` が空であることだけでは「切っていた」と区別できない。
    # thinking は content と別のフィールドで返るので JSON は汚れないが、時間と `num_ctx`
    # を消費し出力に影響する。**捨てると監査できない。**
    think: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        comment="thinking を要求したか（null は未記録＝この列より前の窓の記録）",
    )
    thinking: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="モデルが返した推論本文（think=False なら空。null は未記録）",
    )

    # **接続先**。所要時間はマシン依存なので、時間だけでは比較できない（別マシン・
    # 別モデルの実測は当てはまらない）。どのマシンの値かを記録に残す。
    endpoint: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="呼び出し先の base URL（取れなければ null）",
    )

    # **所要時間**。自動生成にかかった時間（1項目あたりの秒数を、使ったモデルごとに比べる）の
    # **唯一の出所**である。行は生成後に1度だけ作られるので
    # `created_at`/`updated_at` の差からも出せない。**生成の瞬間にしか捕まえられない。**
    #
    # JSON なのは Ollama の内訳（`total_duration` / `load_duration` / `eval_count` /
    # `eval_duration` / `prompt_eval_count` / `prompt_eval_duration`、いずれもナノ秒）を
    # 列を足さずに残すため。**`load_duration` が別に取れるのが要点**——初回コールの
    # モデルロードを差し引けないと、逐次で回したとき1本目のモデルだけ遅く見える。
    timing: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="所要時間（elapsed_ms とサーバ側の内訳。null は未記録）",
    )

    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="生成を実行した操作者",
    )

    questionnaire: Mapped[Questionnaire] = relationship("Questionnaire")
    created_by: Mapped[User | None] = relationship("User")


# 注: 旧 ResourceKeyBinding（resource_key_bindings テーブル）と bindings_for/upsert_binding は
# 廃止した。患者キー列への key_type 付与は indexer（`indexer/`）のワークフロー
# （draft → 人確認 → ingest）へ一本化し、確定済み key_type は索引成果物に焼き込む。
# テーブル削除は alembic migration e7f8a9b0c1d2 を参照。ランタイムのキー解決は DSL の
# step.physical_format を用いるため本廃止の影響を受けない。
