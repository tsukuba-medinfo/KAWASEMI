"""AI入力支援モジュールのPydanticスキーマ"""

import re
from typing import Any, Literal, get_args

from pydantic import AliasChoices, BaseModel, Field, field_validator

from kawasemi.ai_input.models import MAX_SQL_LENGTH
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# study_id は params 経由でセレクタスロット（path_pattern/path/fhir_query）へ差し込めるため、
# patient_id が FHIR クエリ構築時に受けている制約（executor._op_fhir の
# `re.fullmatch(r"[A-Za-z0-9._-]{1,64}", patient_id)`）と揃える。
_STUDY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def validate_study_id(value: str) -> str:
    """study_id の長さ・字種を検証し、前後の空白を除いた値を返す。

    未指定（空文字）は許可する。バッチ依頼者の入力で操作できる値なので、
    evidence へ入る前（受け口）で正規化する。
    """
    value = value.strip()
    if value and not _STUDY_ID_RE.fullmatch(value):
        raise ValueError("study_id の形式が不正です（英数字・.-_のみ、64文字以内）")
    return value


class ClinicalAnnotationCitation(BaseModel):
    """ステップ1(役割1): 臨床記述の典拠1件

    unit_id は索引側のアドレス。reference 文書なら
    `file:{パス}#p{ページ}:{開始文字}-{終了文字}`。payload は索引由来の機械可読情報
    （label / snippet / path / page 等）で、手入力時は抜粋のみを持つことがある。

    llm_model は典拠を差し込んだ候補探索AI由来の来歴。AI案由来の典拠は生成モデル名を
    持ち回り、確認書の「設計過程におけるAI利用」開示欄に表示する。手入力は null。
    人間が記述を編集しても由来の事実は変わらないため保持する。
    """

    unit_id: str | None = None
    payload: dict[str, Any] | None = None
    llm_model: str | None = None


class ClinicalAnnotationIn(BaseModel):
    """ステップ1(役割1): 1項目の臨床的コンテキストアノテーション入力"""

    link_id: str
    item_text: str | None = None
    annotation: str | None = None
    citations: list[ClinicalAnnotationCitation] = []
    llm_model: str | None = None
    # 「規約文書に該当箇所が無い」と**人が**判断した記録。記述を空欄のまま
    # 残す運用で、未着手と区別するために使う。AI は書き込まない。
    no_citation_reason: str | None = None


class ClinicalAnnotationSaveRequest(BaseModel):
    """ステップ1(役割1): 臨床的コンテキストアノテーションの保存リクエスト"""

    items: list[ClinicalAnnotationIn]


class ClinicalAnnotationDraftRequest(BaseModel):
    """ステップ1(役割1): 臨床的意味のAI下書き作成リクエスト"""

    model: str | None = None
    # 項目単位再生成時のみ使用。未指定（一括）なら未入力項目すべてが対象。
    link_id: str | None = None
    # **特定の複数項目だけ**を対象にする。`link_id` と同じく、既存入力の
    # 有無に依らず引き直す。一括生成で一部の項目が未試行に終わった
    # とき、一括（＝未入力の全項目）でも単項目でもない対象の指定が要る。
    #
    # **単項目に落とさないことが目的である。** モデルによっては1項目だけを渡すと応答を
    # 返さなくなることがある（一括なら返る）。項目が減ると入力が短くなり、空いた予算を
    # thinking がそのまま食いうる（手順書経路と同じ現象）。
    link_ids: list[str] | None = None


class LLMClinicalDraft(BaseModel):
    """ステップ1(役割1): LLMが返す1項目の臨床的意味（structured output 受け皿）

    LLM は**典拠の文字列を書かない**。選んだ候補の cand_id を返すだけで、典拠の
    unit_id / payload / 出典表記は KAWASEMI 側が索引から決定的に組み立てる。
    chosen_cand_id=None は「規約文書に該当箇所が無い」を表し、その項目は
    「典拠なし」として区別する。
    """

    link_id: str
    chosen_cand_id: int | None = None
    annotation: str | None = None


class LLMClinicalDrafts(BaseModel):
    """ステップ1(役割1): LLMが返す全項目の臨床的意味（structured output 受け皿）"""

    drafts: list[LLMClinicalDraft]


class ClinicalAnnotationDraftCandidate(BaseModel):
    """ステップ1(役割1): 典拠候補1件

    規約文書（file_role=reference）のチャンク。`source` と `unit_id` / `payload` は
    索引から決定的に導出したもので、LLM は cand_id を選ぶだけである。
    """

    # **LLM へ提示したときの通し番号**。`draft()` 全体を通した連番で、
    # モデルはこの番号で候補を選ぶ（`chosen_cand_id`）。
    #
    # **測定の再現に要る。** 施設側の測定スクリプトは
    # 「同じ順序で振り直せば `cand_id → unit_id` が復元できる」という前提で**索引を
    # 再検索して候補集合を作り直していた**。索引の内容が同じでも埋め込みの数値が僅かに
    # 動けば順位が入れ替わりうるので、**`cand_id` が範囲内で別の unit へ解決しても
    # 誰も検出できない**（`_TOP_K` を触ると一致率が大きく崩れても、記録の見た目には
    # 出ない）。**提示した番号をそのまま記録する。**
    cand_id: int | None = None
    unit_id: str
    source: str
    snippet: str | None = None
    payload: dict[str, Any] | None = None
    # 来歴: 典拠を差し込んだ生成モデル名（ルート側で刻印し、保存まで持ち回る）。
    # ClinicalAnnotationCitation と同じ形にしてあるので、そのまま citations へ保存できる。
    llm_model: str | None = None
    score: float | None = None
    stars: int = 0
    recommended: bool = False


class DraftState:
    """臨床的意味のAI下書き1件の**三状態**

    「典拠つき」「典拠なし」「未試行」の区別は「規約文書を引いても
    意味が決まらなかった項目」を数える人が読むものである。**どれを表示するかの判定を
    画面の JavaScript に置かない**——JS のテストランナーが無いのでレンダリング結果への
    文字列検査で代替することになり、**2つの枝の中身を入れ替える変更が全テストを通る**
    （実測）。判定は API が返し、画面は対応表を引くだけにする。

    - `WITH_CITATION`: 典拠が選ばれた
    - `NO_CITATION`: 探索したが規約文書に該当箇所が無かった
    - `UNATTEMPTED`: 候補はあったが下書きが返らなかった（打ち切り／応答漏れ）。
      **「該当が無い」という結果ではない**ので `NO_CITATION` と混ぜない

    **この三状態はその項目数の分子ではない。** **LLM 経路で典拠が付かなかった項目を
    「意味が決まらなかった項目」として報告してはいけない**。`NO_CITATION` が畳み込む分類のうち
    「定義が候補に無く null」「候補が0件」「矛盾した応答（重複エントリ）」の3つは
    明示的に分子から外す（この取り違えは過去に2度起きている）。
    その項目数を数えるのは人であり、**この画面はその人が「どこを見るか」を決める材料**
    であって分子そのものではない。
    """

    WITH_CITATION = "with_citation"
    NO_CITATION = "no_citation"
    UNATTEMPTED = "unattempted"
    ALL = (WITH_CITATION, NO_CITATION, UNATTEMPTED)


class ClinicalAnnotationDraftItem(BaseModel):
    """ステップ1(役割1): 1項目分の臨床的意味のAI下書き

    annotation は LLM が書いた記述。citation は LLM が選んだ候補（recommended=True）で、
    **選べなかった場合は None** になる。`no_citation` はその区別を明示するフラグで、
    「規約文書を引いても意味が決まらなかった項目」の可視化に使う。

    candidates は検索で得た候補の一覧（提示順。LLM 一推しを先頭へ繰り上げ）。
    人が別の候補へ差し替えられるように全件返す。

    **`score` の降順とは限らない**。索引経路は型が一致した候補を加点して
    並べ替えるので、`score`（素の cosine）と `stars` は並びに対して単調でない。
    """

    link_id: str
    annotation: str | None = None
    citation: ClinicalAnnotationDraftCandidate | None = None
    candidates: list[ClinicalAnnotationDraftCandidate] = []
    no_citation: bool = True


# 1項目あたりに保存できるデータ所在候補の**上限**。
#
# 上限が無いと、`designer` 権限と CSRF トークンさえあれば API 直叩きで任意件数の候補が
# 入る。**コンパイラ（ステップ3）が読むのは主候補と代替 `compiler._MAX_ALT_CANDIDATES` 件
# だけ**なので、それを超える候補は保存できても誰も読まない——記録には残るのに取得計画には
# 現れない候補ができる。読まれない値を記録に残さないため、受け口で 422 にする。
#
# `compiler._MAX_ALT_CANDIDATES` はこの値から導く（正本はここ側にある。`schemas` は
# `compiler` を import しない＝依存の向きを増やさない）。
MAX_LOCATION_CANDIDATES = 3


class SystemLocationCandidate(BaseModel):
    """ステップ1(役割2): データ所在の1候補

    location は人間可読の所在記述。unit_id/connection_id/payload はリソース索引
    由来の機械可読情報で、ステップ3が op 引数を決定的に導出するための素材
    （手入力時は null）。

    llm_model は候補探索AI（AI案）由来の来歴。AI案で差し込まれた候補は生成モデル名を
    持ち回り、確認書の「設計過程におけるAI利用」開示欄に表示する。手入力候補は null。
    人間が location を編集しても由来の事実は変わらないため保持する。
    """

    # 種別の**値**はここでは検査しない（`routes._clean_candidate` が未知の種別を
    # `manual` へ落とす。fail-closed 側なので受け口で 422 にする必要が無い）。
    # null を許すのは AI 案由来の候補をそのまま送り返す経路があるため（既定は "manual"）。
    source_category: str | None = "manual"
    location: str | None = None
    unit_id: str | None = None
    connection_id: str | None = None
    payload: dict[str, Any] | None = None
    llm_model: str | None = None


class SystemLocationIn(BaseModel):
    """ステップ1(役割2): 1項目のデータ所在入力（優先順の候補配列）

    旧形式（`candidates` を持たず `source_category` / `location` などを平坦に持つ）も
    受理する。**`candidates` の未指定（None）と空配列は区別する**——空配列は「候補を
    全部消す」という明示の保存であって、旧形式へのフォールバックではない
    （`routes._normalize_candidates` が同じ区別で分岐する）。
    """

    link_id: str
    item_text: str | None = None
    candidates: list[SystemLocationCandidate] | None = Field(
        default=None, max_length=MAX_LOCATION_CANDIDATES
    )
    # 旧形式の平坦なフィールド（`candidates` が未指定のときだけ読まれる）
    source_category: str | None = None
    location: str | None = None
    unit_id: str | None = None
    connection_id: str | None = None
    payload: dict[str, Any] | None = None


class SystemLocationSaveRequest(BaseModel):
    """ステップ1(役割2): データ所在の保存リクエスト"""

    items: list[SystemLocationIn]


class LocationAssessmentIn(BaseModel):
    """データ所在の確からしさの判定1件の記録リクエスト

    cand_key を省略した場合は主たる候補（candidates[0]）を対象にする。
    stage は AssessmentStage（1〜4）のいずれか。is_undeterminable=True の場合は
    stage を指定しない（段階を付けず「判定不能」として記録する）。
    """

    link_id: str
    cand_key: str | None = None
    stage: int | None = None
    is_undeterminable: bool = False
    rationale: str
    unreachable_reason: str | None = None
    # 様式の版や対象群によって所在が変わるか。旧名 `cancer_type_dependent_note` も受け付ける
    variant_dependent_note: str | None = Field(
        default=None,
        validation_alias=AliasChoices("variant_dependent_note", "cancer_type_dependent_note"),
    )
    materials: dict[str, Any] | None = None


class SystemLocationDraftRequest(BaseModel):
    """ステップ1(役割2): データ所在のAI案作成リクエスト（LLMモデル指定可）"""

    model: str | None = None
    # 項目単位再生成時のみ使用。未指定（一括）なら空欄項目すべてが対象。
    link_id: str | None = None
    # **特定の複数項目だけ**を対象にする（臨床側と同じ形）。所在側も1コールの
    # 項目数が減れば同じ現象（thinking が空いた予算を食う）に当たるので、未試行項目の
    # 埋め直しに備えて同じ手段を置く。
    link_ids: list[str] | None = None


class LLMLocationChoice(BaseModel):
    """ステップ1(役割2): LLMが返す1項目のカラム選択（structured output受け皿）

    LLMは候補の cand_id を選ぶだけ。所在文字列は KAWASEMI 側で組み立てる。
    chosen_cand_id=None は「該当候補なし（未入力スキップ）」を表す。
    """

    link_id: str
    chosen_cand_id: int | None = None
    source_category: str = "manual"


class LLMLocationChoices(BaseModel):
    """ステップ1(役割2): LLMが返す全項目の選択（structured output受け皿）"""

    choices: list[LLMLocationChoice]


class LLMLocationHint(BaseModel):
    """ステップ1(役割2): 検索前に LLM が書く「院内での呼ばれ方」と想定型

    索引を検索する**前**に生成する。項目名と臨床的意味はマニュアルの言葉で書かれて
    いるが、索引側の DB カラムは略号なので、そのままでは字面が寄らない
    （略号の列名しか持たない DB 側が検索で負けていた）。

    `terms` はクエリ文へ連結して埋め込むだけで、**所在文字列にはならない**。
    `expected_types` は候補の `payload.type_raw` との一致で加点にだけ使う。
    """

    link_id: str
    # 院内システムでこの値が入っている表・カラム・文書の呼ばれ方（略号を含む）
    terms: list[str] = []
    # 想定される型と桁（`CHAR(8)` / `VARCHAR` / `NUMERIC` など索引の type_raw の語彙）
    expected_types: list[str] = []


class LLMLocationHints(BaseModel):
    """ステップ1(役割2): LLMが返す全項目の検索ヒント（structured output受け皿）"""

    hints: list[LLMLocationHint]


class SystemLocationDraftCandidate(BaseModel):
    """ステップ1(役割2): top-k選択UIへ提示するデータ所在候補1件

    索引/BM25検索で見つかった候補。ユーザはこの一覧から1件を選んで確定する。
    unit_id/connection_id/payload は索引由来の機械可読情報（選択・保存されると
    ステップ3が op 引数を決定的に導出する素材になる）。BM25フォールバック時は null。

    score は検索の生スコア（索引=cosine 0〜1 / BM25=非正規。経路で尺度が異なる）。
    stars はそれを経路別閾値で 1〜3 段階に正規化した「適合度」表示用の値。
    recommended は LLM が一推しした候補（並びの先頭に来る。初期選択＋AI推奨バッジ用）。
    """

    source_category: str
    location: str | None = None
    unit_id: str | None = None
    connection_id: str | None = None
    payload: dict[str, Any] | None = None
    # 来歴: 差し込みに使った生成モデル名（ルート側で刻印し、保存まで持ち回る）
    llm_model: str | None = None
    score: float | None = None
    stars: int = 0
    recommended: bool = False
    # **検索で当たった候補ではない**。診療記録の本文への受け皿として、
    # 上位 `_TOP_K` 件の**後ろへ足した**候補であることを表す。
    #
    # AI 提案の適合率は**検索の性能**を測る指標なので、この印が付いた候補は
    # 分子からも分母からも外す。`proposed_candidates` に保存されるので、あとから機械的に
    # 分けられる。**表示の都合ではなく測定の都合で持つフィールドである。**
    fallback: bool = False


class SystemLocationDraftItem(BaseModel):
    """ステップ1(役割2): 1項目分のAI案（top-k候補の一覧）

    candidates は**提示順**。索引経路は型が一致した候補を加点して並べ替えるので、
    **適合度（score）の降順とは限らない**——`score` は素の cosine のままなので、★2 が ★3 の上に来ることがある。BM25 経路は従来どおり score 降順である。
    ただし LLM が一推しした候補があれば、それを先頭に繰り上げる（recommended=True）。
    一推しが無い項目（LLMが該当なし判断）は全候補 recommended=False のまま並ぶ。
    """

    link_id: str
    candidates: list[SystemLocationDraftCandidate] = []


# 手順書ステップの種別タグ。ステップ3が DSL へコンパイルするときの手掛かりで、
# 値そのものではない。**この定数が語彙の正本**である（プロンプト・UI のセレクタ・デモ手順書は
# これと一致していることをテストで固定する。4箇所に散らすと必ずずれる）。
#
# `condition` / `branch` は後から足した。他のレイヤー（DSL の `compare` / `branch`、
# 臨床的意味の「条件分岐」、コンパイラのプロンプトの「分岐条件の根拠」）には分岐の概念が
# あるのに手順書レイヤーだけ欠けており、自然言語の手順書から分岐を含む DSL へ変換する経路が
# 語彙レベルで切れていた。
#
# **タグは手順の意味であって、ステップ3がどの op で実装するかの予告ではない。** 例えば
# 「行があれば1・無ければ2」は `condition`（判定）＋`branch`（判定に応じた値の選択）で書く。
# それを SQL の CASE 1本で実装するか `compare`→`branch` の2 op で実装するかはステップ3の
# 裁量で、手順書を書く人（および生成する LLM）にはその区別が見えない。op を予告させると
# **同じ日本語の手順に別のタグが付く**（学習できない区別になる）。
#
# `select` は同一の取得結果から1件を選ぶ・絞り込むこと。**所在候補の選択は `select` では
# なく `condition` + `branch`** である（所在候補の優先順はフォールバック取得計画へ
# 写る）。一方、**同一の取得結果の中でのカラム代替**（例 [11] 重症度分類の
# `COALESCE(severity, severity_code)`）は「どこから取るか」ではないので `select` に留める。
RunbookActionType = Literal[
    "fetch",  # データ取得
    "select",  # 取得結果から1件を選ぶ・絞り込む
    "transform",  # 変換・単位換算・コード変換
    "llm",  # 自由記述からの抽出を LLM に任せる
    "manual",  # 人手確認
    "condition",  # どちらの経路を採るかの判定
    "branch",  # 判定に応じて取得元・値を選ぶ分岐
]
# Literal を正本にして tuple を導出する。Literal 側が正本なのは、これが structured output の
# JSON スキーマ（`response_format`）の enum に載り、**LLM の制約デコードへ届く**ため。
# tuple だけだと語彙はモデルに伝わらず、外れた綴りが黙って None へ落ちるだけになる。
RUNBOOK_ACTION_TYPES: tuple[str, ...] = get_args(RunbookActionType)


class RunbookStep(BaseModel):
    """ステップ2: 手順書の1ステップ（NL中心＋軽い構造）"""

    order: int
    title: str
    description: str = ""
    # 語彙は RUNBOOK_ACTION_TYPES。語彙外は None へ落とす（下の検証を参照）。
    # `mode="before"` の検証が Literal の検査より先に走るので、Literal 化しても
    # 「弾かずに落とす」性質は保たれる（非文字列が来ても例外にならない）。
    action_type: RunbookActionType | None = None
    data_hint: str | None = None

    @field_validator("action_type", mode="before")
    @classmethod
    def _normalize_action_type(cls, value: Any) -> str | None:
        """語彙外のタグを None にする（人手の保存でも LLM 出力でも同じ扱い）。

        **弾かずに落とす。** このモデルは LLM の structured output の受け皿でもあり、
        1つのタグの綴り違いで**チャンク全体の生成が失敗する**のは割に合わない。タグは
        自由文（title / description）の手掛かりであって、値そのものではない。
        大文字・前後の空白は吸収する（`"Fetch "` → `"fetch"`）。

        `mode="before"` であることが要点。`after` にすると Literal の検査が先に走り、
        LLM が `["condition"]` や `5` のような非文字列を吐いた瞬間に **チャンク全体が
        失敗する**（この検証の目的そのものが失われる）。
        """
        if value is None:
            return None
        text = str(value).strip().lower()
        if not text:
            return None
        if text not in RUNBOOK_ACTION_TYPES:
            # 任意長の入力がそのままログに載らないよう切る（`!r` で改行はエスケープされる）
            logger.warning(f"手順書ステップの未知の action_type を無視します: {str(value)[:64]!r}")
            return None
        return text


class GeneratedRunbookItem(BaseModel):
    """ステップ2: Ollamaが生成する1項目の手順書（structured output受け皿）"""

    link_id: str
    steps: list[RunbookStep] = []


class GeneratedRunbooks(BaseModel):
    """ステップ2: Ollamaが生成する全項目の手順書（structured output受け皿）"""

    runbooks: list[GeneratedRunbookItem]


class RunbookGenerateRequest(BaseModel):
    """ステップ2: 手順書の一括生成／項目再生成リクエスト（LLMモデル指定可）"""

    model: str | None = None


# 楽観的排他の版トークン。画面は読み込んだ（最後に保存した）時点の `version` を送り、
# サーバの行がその後に変わっていれば 409 を返す。行がまだ無いと思っている画面は null を
# 送る。**確認（confirm=true）と下書きへ戻す操作では必須**（無ければ 428）。下書き保存
# だけは省略でき、省略すると検査しない（API 直叩き・移行スクリプトとの互換）。
_BASE_VERSION_DOC = (
    "読み込んだ時点の版（null は「行がまだ無い」）。確認・下書きへ戻す操作では必須、"
    "下書き保存では省略可（省略すると検査しない）"
)


class RunbookSaveRequest(BaseModel):
    """ステップ2: 1項目分の手順書ステップ保存リクエスト"""

    steps: list[RunbookStep] = []
    base_version: str | None = Field(default=None, description=_BASE_VERSION_DOC)


# =============================================================================
# ステップ3: 手順書 → DSLプログラムのコンパイル
# =============================================================================


class DSLStep(BaseModel):
    """DSLプログラムの1命令（op）

    全opで共通の op/id/needs_input/reasoning/guard に加え、op固有の引数を任意フィールド
    として持つ（union-of-fields 方式）。値を取る引数（left/right/when/then/else_branch/
    input/source/guard）は "$id" 形式の参照、またはリテラルを受け付ける。
    """

    op: str
    id: str
    needs_input: bool = False
    reasoning: str | None = None

    # --- 条件付き実行（全 op 共通） ---
    # 真のときだけこの step を実行する条件。"$id" 参照（前段 step の結果）または
    # 真偽リテラル。未指定（None）は無条件実行。偽なら step は実行されず（当該 step の
    # データ取得・LLM 呼び出しを一切行わず）trace に skipped=true として記録される。
    # 「実行するか否か」を決めるのが guard、「計算済みの値のどちらを採るか」を決めるのが
    # branch であり、役割が異なる。
    #
    # bool も受けるのは、プラン編集 UI で手書きされた `"guard": false`（その step を
    # 恒久的に無効化する書き方）が 422 で落ちないようにするため。任意の文字列リテラルは
    # dsl.validate_program が拒否する（"$" 落ちで guard が黙って no-op になるのを防ぐ）。
    guard: str | bool | None = None

    # --- 抽出系（sql_extract / external_db_extract / file_extract） ---
    sql: str | None = Field(default=None, max_length=MAX_SQL_LENGTH)
    # 接続レジストリ（connections.yaml）の接続ID。executor はこの名前で DSN/
    # root_path を解決する。external_db_extract / file_extract で使用。
    connection_id: str | None = None
    # connection_id の旧フィールド名。executor は後方互換で今も読むため
    # （`step.get("connection_id") or step.get("connection")`）、スキーマ側にも定義が
    # 必要（未定義だと pydantic 経由の保存で黙って捨てられ、旧プランが壊れる）。
    # 新規プランでは connection_id を使うこと。
    connection: str | None = None  # deprecated: connection_id を使う
    multi: bool = False  # true のとき行リストを返す（select/aggregate の入力用）

    # --- 抽出系（file_extract） ---
    path_pattern: str | None = None  # 接続 root_path 起点の相対パス。例: {{patient_id}}/report.pdf
    # 先頭 "/" の絶対パスは拒否される。glob（*）は展開されない
    extract: dict[str, Any] | None = None  # 抽出方法 {method: pdf_llm 等}

    # --- 抽出系（fhir_extract） ---
    fhir_query: str | None = None  # 例: /Observation?patient={{patient_id}}
    # 名前に反して FHIRPath ではない。executor.resolve_fhir_path が解釈する独自の
    # 簡易ドット記法で、対応するのは "." 区切りの辿りと "name[0]" 形式の添字のみ
    # （先頭のリソース型は読み飛ばす）。where()/ofType()/as/| 等の FHIRPath 構文、
    # 関数・演算子・条件式は一切解釈しない。詳細は docs/dsl_specification.md §5.2。
    fhir_path: str | None = None  # 例: Observation.valueQuantity.value

    # --- 抽出系（csv_extract; 全患者横断CSVを決定的に行絞り＋列取得） ---
    # path は connection の root_path 起点の固定パス（patient_id 置換なし）。
    # 列指定は「文字列＝ヘッダ名 / 整数＝0始まりindex」の両対応。header=false の
    # ヘッダ無しCSV（DPCのEF/D等）は列indexで指定する。
    path: str | None = None  # 例: EF_2025.csv（root_path 起点・固定）
    key_column: str | int | None = None  # 患者ID列（解決済みキーと厳密文字列等値）
    column: str | int | None = None  # 取り出す列
    # 複合等値AND [{column, value}]（value は $id/リテラル）。value は非PHIの識別子
    # （入院ID/診療行為コード等）を想定し evidence に確定値を残す。患者本人のキー
    # （MRN等）は match でなく key_column（解決・マスク経路）で渡すこと。
    match: list[dict[str, Any]] | None = None
    header: bool = True  # 先頭行をヘッダ名として扱うか
    delimiter: str | None = None  # 区切り文字（既定 ","、タブ区切りは "\t"）

    # --- LLM系（llm_extract / llm_judge） ---
    prompt: str | None = None
    # step 単位のモデル固定。executor の選択は「実行時指定 → step.llm_model → 既定」の
    # 3段（executor._op_llm）。InputPlan.llm_model（コンパイルした由来モデル）とは別物。
    llm_model: str | None = None

    # --- compare ---
    # `is_empty` / `is_not_empty` は **`right` を取らない単項演算子**。
    # 空の定義は `executor.is_empty_value`（None / "" / []）で、`guard` と同じ規則。
    left: Any | None = None
    cmp: str | None = None  # < <= > >= == != in contains is_empty is_not_empty
    right: Any | None = None

    # --- branch ---
    when: Any | None = None
    then: Any | None = None
    else_branch: Any | None = None

    # --- transform ---
    input: Any | None = None  # 変換対象の値（参照/リテラル）
    rule: dict[str, Any] | None = None  # code_map / unit_conversion

    # --- 所在の典拠（ステップ3がコード側で刻印。LLM の出力は採用しない） ---
    # データ所在（SystemLocation.candidates）の索引ユニットID。取得系 op の引数を
    # どの索引ユニットの payload から確定したかを残し、DSL から所在の典拠を辿れる
    # ようにする。コンパイラが上書きするため LLM が書いた値は破棄される。
    unit_id: str | None = None

    # --- select / aggregate ---
    source: str | None = None  # multi 抽出 step への "$id" 参照
    # where / order は人間可読の設計メモであり、実行時に評価されない（executor._op_select
    # は pick=first/last のみ実行する）。where は監査証跡（trace.inputs）へ記録されるが
    # 行の絞り込みには使われず、order は並び替えを一切行わない。決定的な絞り込み・並び順は
    # 抽出側（SQL の WHERE / ORDER BY、csv_extract の match、FHIR クエリ）で表現すること。
    where: str | None = None  # select: 絞り込み条件（自由記述ヒント・非実行）
    order: str | None = None  # select: 並び順ヒント（自由記述・非実行）
    pick: str | None = None  # select: first / last
    func: str | None = None  # aggregate: count / min / max / avg / first

    # --- キー解決（データ取得系 op 共通; docs/key_resolution_guide.md） ---
    # key_type を宣言すると、実行前に resolver が登録番号から当該名前空間の値を
    # 一括解決し（executor._resolve_keys）、physical_format を適用した値を op の
    # 患者キーへバインドする（executor._key_for_step）。未指定なら登録番号をそのまま
    # 使う（後方互換）。physical_format の対応キーは pad / padchar / prefix / suffix。
    key_type: str | None = None  # 例: mrn / fhir_patient_id
    physical_format: dict[str, Any] | None = None  # 例: {"pad": 10}


class DSLParam(BaseModel):
    """DSLプログラムの引数（params）1件の宣言

    op のテンプレートへ安全に値を差し込むための宣言。差し込み先（スロット）ごとに
    executor が規定のサニタイズを適用する（ParamBinder）。

    - name:  参照名。SQL では ``:name``（バインド値）または ``{{name}}``（識別子）、
             fhir_query / path_pattern / path / prompt では ``{{name}}`` で参照する。
    - kind:  ``value``（既定。バインド値・FHIR検索値・パス構成要素として使う）か
             ``identifier``（SQL の表名・カラム名としてテキスト展開する）。
             identifier はバインドで表現できない唯一の文字列連結経路なので、
             厳格な識別子字種＋allowed の二重の関門を通る。
    - value: リテラル（文字列/数値/真偽）か ``"$id"``（前段 step の出力を参照）。
             ``"$id"`` を使うと「まず区分値を取得し、それで参照表名や FHIR の code=
             を組み立てる」といった段階的な組み立てができる。
    - allowed: 許容値の許可リスト。字種検査は「形」しか縛らないため、**実行時の
             データが読み取り対象を選べる場合は宣言が必須**（未宣言は検証エラー）。
             必須になるのは次の2つ。
             (1) kind=identifier のパラメータ（宣言の時点で必須）。
             (2) value が ``"$id"``（前段 step の出力）で、それをセレクタスロット
                 （sql の ``{{name}}`` / path_pattern / path / fhir_query）へ
                 差し込む場合。
             SQL の ``:name`` バインドと prompt は対象外。バインドはドライバが
             パラメータ化するので表を切り替えられず（選べるのは「どの行か」だけ）、
             prompt は読み取り対象ではなく解析されるデータであるため。

    組み込みパラメータ patient_id / study_id は宣言不要で参照でき（study_id は
    バッチのジョブか autofill の要求本文から供給され、空なら参照した step が失敗する）、
    再宣言はできない（なりすまし防止）。
    """

    name: str
    kind: str = "value"
    value: Any | None = None
    allowed: list[str] | None = None
    description: str | None = None


class DSLProgram(BaseModel):
    """DSLプログラム本体: 引数 params、順序付き steps、最終値参照 return"""

    params: list[DSLParam] = []
    steps: list[DSLStep] = []
    return_: str | None = Field(default=None, alias="return")  # 最終的に項目値として採用する "$id"

    model_config = {"populate_by_name": True}


class GeneratedPlanItem(BaseModel):
    """ステップ3: Ollamaが生成する1項目のDSLプログラム（structured output受け皿）"""

    link_id: str
    program: DSLProgram


class GeneratedPlans(BaseModel):
    """ステップ3: Ollamaが生成する全項目のDSLプログラム（structured output受け皿）"""

    plans: list[GeneratedPlanItem]


class PlanCompileRequest(BaseModel):
    """ステップ3: 一括コンパイル／項目再コンパイルリクエスト（LLMモデル指定可）"""

    model: str | None = None


class PlanSaveRequest(BaseModel):
    """ステップ3: 1項目分のDSLプログラム保存リクエスト"""

    program: DSLProgram
    base_version: str | None = Field(default=None, description=_BASE_VERSION_DOC)


class PlanDemoteRequest(BaseModel):
    """ステップ3: 確認済みの取得プランを draft へ戻すリクエスト"""

    base_version: str | None = Field(default=None, description=_BASE_VERSION_DOC)


class AutofillRequest(BaseModel):
    """ステップ4（旧ステップ3）: 自動入力実行リクエスト"""

    patient_id: str  # 被験者の登録番号（Hub）。resolver が各システムの患者キーへ解決する。
    study_id: str = ""  # 試験識別子。(study, subject) で被験者を一意化する（未指定可）。
    model: str | None = None

    @field_validator("study_id")
    @classmethod
    def _validate_study_id(cls, value: str) -> str:
        return validate_study_id(value)


class EvidenceItem(BaseModel):
    """ステップ4（旧ステップ3）: 1項目の根拠情報"""

    source_type: str
    query_used: str | None
    raw_data: Any
    path_used: str | None
    reasoning: str | None


class AutofillResponse(BaseModel):
    """ステップ4（旧ステップ3）: 自動入力実行レスポンス"""

    success: bool
    prefill_data: dict[str, Any]
    evidence: dict[str, EvidenceItem]
    status: str
    execution_id: int | None = None
