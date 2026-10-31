"""設計ロールの LLM 来歴の収集と保存

実行ロール（`llm_extract` / `llm_judge`）は evidence trace にプロンプト全文と生出力を
残すのに対し、設計ロールは `llm_model` の文字列しか残していなかった。「LLM が何を
読んでその記述にしたか」を後から確認できない状態だったので、設計ロールにも同じ粒度の
来歴を持たせる。

**保存してよい根拠は入力の性質である。** 設計ロールの入力は次のとおりで、患者個票を
含まない（論証は `docs/design_llm_provenance.md`）。

| 設計ロール | 入力 |
|---|---|
| 臨床的意味 | 規約文書（公開文書）＋項目名 |
| データ所在 | 索引（スキーマ定義・マスタ・reference 文書）＋項目名 |
| 取得手順書 | 項目名・所在記述・臨床的意味 |
| 取得プラン | 上記＋確認済み手順書 |

この前提が崩れる経路に対して本モジュールが持つのは、ガード2つと記録1つである。

1. **役割の許可リスト（ガード）。** `DesignRole.ALL` 以外の role では保存しない
   （例外にする）。実行ロールの来歴がこのテーブルへ流れ込む経路を構造的に閉じる。入口
   （`DesignLlmRecorder.__init__`）と保存時（`save_design_llm_calls`）の両方で検査する
2. **内容由来テキストの出所（記録＋不変条件検査）。** プロンプトへ**原資料の中身**を
   埋め込んだ設計ロールは、その出所を `declare_content_sources` で宣言し、
   `content_sources` 列に残す。**これは主として監査のための記録である**（後述のとおり、
   検査そのものは正しい索引では発火しない）
3. **保存上限。** 1フィールドあたりの上限を超えたら切り詰め、`truncated=True` を立てる。
   切り詰めた事実を残すことで「全文がある」と誤認させない

**ガード2 の実態を過大に書かない。** 初版は `{patient_id}` などのマーカーをプロンプト
全文から探していた。これは正常系で必ず発火し（`build_manifest_units` が作る所在宣言の
`path_pattern` と DSL の `{{patient_id}}` に一致する）、本来の脅威である「本文の抜粋」は
検出できないという二重の誤りだった。作り直した現行版は「どのユニットの中身を
プロンプトへ入れたか」を型で持ち、その `file_role` を検査する。

その結果ガード2 は**索引の不変条件検査**になった。索引の契約では、中身をチャンク化した
ユニット（`build_reference_units`）だけが `char_range` を持ち、その `file_role` は必ず
`reference` である。所在宣言ユニット（`build_manifest_units`）は `char_range` を持たず
中身も持たない。したがって **indexer（`indexer/`）が作る正しい索引では、このガードは原理的に
発火しない**。発火するのは索引の作り方かプロンプト構築が壊れたときだけである。

**「患者データを含む文書が reference として索引される」という本来の脅威を、この層は
検出できない。** その文書は正しく `file_role=reference` の中身つきユニットになるので、
ガード2 の検査を素通りする。この脅威は次の2つで担う。

- 索引側の入口制限（`docs/resource_index_guide.md` が `--file-source` に定めた
  「患者データを含まない共通文書のみ」）と、その規則を破らない同梱構成
- `content_sources` 列の記録（事後の監査で「どの種類の資料の中身を読んだか」を追える）

同様に、人が自由記述する臨床的意味の記述・手順書の説明欄・所在候補の手入力に患者の
実データを書いた場合も検出できない。**これらは索引側の入口制限と運用（記入者への周知）で
担保する範囲であり、本モジュールの守備範囲ではない**（`docs/design_llm_provenance.md` に
同じ整理を書いた）。

`llm_json` の PHI 保護方針（ログ・例外に生応答を載せない）は変えていない。本モジュールは
**ログでも例外でもなく DB** へ記録する別経路である。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.llm_json import LLMCallRecord
from kawasemi.ai_input.llm_provider import model_digest_of
from kawasemi.ai_input.models import DesignLlmCall, DesignRole
from kawasemi.ai_input.resource_index.schema import Kind, ResourceUnit
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# 1フィールドの保存上限。プロンプトは num_ctx 8192 トークン相当で、規約文書の抜粋を
# 数件含む。日本語で余裕を見た上限を置き、超過分は切り詰めて事実を記録する。
MAX_FIELD_CHARS = 64_000

# 中身をプロンプトへ入れてよいファイルの file_role。索引はこの値のファイルだけ
# チャンク化して snippet に中身を入れる（indexer の `build_reference_units`）。
CONTENT_ALLOWED_FILE_ROLE = "reference"


class DesignProvenanceError(RuntimeError):
    """来歴として保存してはならない内容を検出した（ガード）。"""


class DesignLlmRecorder:
    """`chat_json` / `chat_json_chunked` に渡す来歴の受け取り口。

    収集だけを行い、DB へは書かない（LLM 呼び出しの最中に DB セッションを持ち回ると、
    呼び出しの失敗と保存の失敗が絡まる）。呼び出しが終わったあとに
    `save_design_llm_calls` へ渡す。
    """

    def __init__(self, role: str) -> None:
        _assert_design_role(role)
        self.role = role
        self.records: list[LLMCallRecord] = []
        # プロンプトへ中身を入れた索引ユニットの出所（"kind:file_role" の集合）。
        self.content_sources: set[str] = set()
        # **検索に使った索引の同一性**。`None` は「照合していない」で、
        # 空リストの `mismatches` は「照合して一致した」である（区別を保つ）。
        self.index_build_info: dict[str, Any] | None = None
        # **同じ項目に複数のエントリが返った link_id**。**サーバ側の真実**として
        # 応答をマージした直後に記録する。空配列は「重複が無かった」、None は「記録していない」。
        self.duplicated_link_ids: list[str] | None = None
        # **使ったモデルのダイジェスト**。タグだけでは実装が特定できない
        # （`ollama pull` で上流が更新されても、同名タグを作り直しても記録は同じ文字列）。
        # **null の意味は2つあるので理由を別に持つ**——`model_digest_unavailable` が入って
        # いれば「引いたが取れなかった（理由つき）」、両方 null なら「宣言していない」。
        self.model_digest: str | None = None
        self.model_digest_unavailable: str | None = None

    def __call__(self, record: LLMCallRecord) -> None:
        self.records.append(record)

    def declare_index_identity(self, store: Any) -> None:
        """検索に使った索引の同一性を宣言する。

        **索引を読んだロールだけが呼ぶ。** 呼ばなければ来歴の列は `null` のままで、
        それは「照合していない」を意味する（空配列の `mismatches`＝「照合して一致した」とは
        別である）。索引は版管理外なので、差し替えは diff にもコードのハッシュにも現れない
        ——**索引が実際に使われた瞬間の記録**だけが同一性を残せる。

        `store` は `ResourceIndexStore`（`build_info` を持つもの）。
        型で縛らず属性で受けるのは、来歴の収集が索引モジュールに依存しないようにするため。
        """
        build_info = getattr(store, "build_info", None)
        if build_info is not None and hasattr(build_info, "model_dump"):
            self.index_build_info = build_info.model_dump(mode="json")
        elif isinstance(build_info, dict):
            self.index_build_info = dict(build_info)
        else:
            # **読めなかったことを残す**（黙って null にすると「索引を読まないロール」と
            # 区別できなくなる）。食い違いの側に理由が入る。
            self.index_build_info = None

    async def declare_model_digest(self, client: Any, model: str) -> None:
        """生成に使ったモデルのダイジェストを宣言する。

        **タグは実装を特定しない。** `OLLAMA_MODEL` は環境変数で上書きできる（`.env` を
        書き換えるだけでコードのハッシュに現れずモデルが変わる）が、
        **同じタグの中身が差し替わる**経路はそこでも残っていた——`ollama pull` で上流が
        更新された場合と、ローカルで同名タグを作り直した場合である。ダイジェストを残せば
        「同じタグで中身が違うモデルを使った2回の実行」を記録だけで区別できる。

        **呼ぶのは生成を実行するロールで、生成の直前に呼ぶ。** 索引の同一性と
        同じ理由で、実際に使われた瞬間の記録だけが同一性を残せる。

        取得できなかった場合は `model_digest` が null のまま `model_digest_unavailable` に
        理由が入る（`llm_provider.model_digest_of` は例外を投げない）。**両方 null は
        「宣言していない」**を意味する——索引の同一性の「空配列と null は意味が違う」と同じ規則で、
        「引いたが取れなかった」を「記録していない」に化けさせない。
        """
        digest, reason = await model_digest_of(client, model)
        self.model_digest = digest
        self.model_digest_unavailable = reason

    def declare_duplicated(self, link_ids: Iterable[str]) -> None:
        """同じ項目に複数のエントリが返った link_id を宣言する。

        **応答をマージした側（サーバ）が記録する。** クライアントの申告に依存しない
        （`origin` の自己申告で得た教訓）。**空配列でも呼ぶ**——「マージして
        重複が無かった」と「記録していない（null）」は違う。
        """
        self.duplicated_link_ids = sorted(set(link_ids))

    def declare_content_sources(self, units: Iterable[ResourceUnit]) -> None:
        """プロンプトへ **原資料の中身** を埋め込んだ索引ユニットを宣言する（ガード2）。

        呼ぶのは「ユニットの**中身**（`snippet` または `search_text`）をプロンプトへ
        入れる」設計ロールだけである。データ所在は `snippet`、臨床的意味は
        `search_text`（チャンク全文）を載せる。項目名・所在記述・確認済み成果物だけを
        プロンプトへ入れるロール（取得手順書・取得プラン）は索引の中身を読まないので、
        宣言する対象を持たない。

        **どちらのフィールドが非空であっても「中身を見せた」ことは意味しない。**
        `kind=file` の `snippet` / `search_text` は二重の意味を持つ。

        - `build_reference_units`（中身をチャンク化）: 本文の抜粋。payload に
          `path` と **`char_range`** を持つ
        - `build_manifest_units`（所在宣言。中身は索引しない）: `path_pattern`
          （＋notes）という索引作成者のメタ情報。payload は `path_pattern` と
          `extract` を持ち、**`char_range` を持たない**

        （所在宣言の `search_text` も label ＋ aliases ＋ notes という索引作成者の
        メタ情報で、原資料の中身ではない。）

        患者データファイルは所在候補として正当なので、後者が候補に入るのは正常系で
        ある。両者をフィールドの有無だけで同一視すると、ガードが正常系で必ず発火する。
        そこで `kind=file` は **`char_range` を持つときだけ中身**として宣言する。

        `db` / `fhir` / `master` の snippet はスキーマ定義・コード表（＝索引作成者が
        書いた定義情報）なので、原資料の中身ではないが、監査で「どの種類の資料を
        読んだか」を追えるよう kind だけ記録する。
        """
        for unit in units:
            if unit.kind == Kind.FILE:
                if "char_range" not in unit.payload:
                    continue  # 所在宣言（マニフェスト）。中身ではないので宣言しない
                # 中身つきユニットは file_role が判定の要なので、欠けていること自体を
                # 記録する（`unset` は reference ではないのでガード2 が拒否する）。
                self.content_sources.add(f"{Kind.FILE}:{unit.payload.get('file_role') or 'unset'}")
            else:
                # db / fhir / master はスキーマ定義・コード表で file_role を持たない。
                self.content_sources.add(unit.kind)


def _assert_design_role(role: str) -> None:
    """実行ロールの来歴がこのテーブルへ入る経路を構造的に閉じる（ガード1）。"""
    if role not in DesignRole.ALL:
        raise DesignProvenanceError(
            f"設計ロール以外の来歴は保存できません: {role!r}（許可: {sorted(DesignRole.ALL)}）"
        )


def _guard_content_sources(sources: set[str], *, role: str) -> None:
    """索引の不変条件「中身つきファイルユニットは reference である」を検査する（ガード2）。

    宣言された `kind=file` のユニット（＝`char_range` を持つ中身つきユニット）で
    `file_role` が reference 以外のものがあれば拒否する。

    **正しい索引では発火しない。** `build_reference_units` は `char_range` と
    `file_role=reference` を必ず同時に付けるので、この組み合わせは索引の契約上
    成立しない。発火するのは索引ビルダかプロンプト構築が壊れたときだけである。
    「患者データを含む文書が reference として索引される」という本来の脅威はここでは
    検出できない（モジュール docstring の整理を参照）。

    `kind=db` / `fhir` / `master` はスキーマ定義・コード表であって原資料の中身ではない
    ので対象にしない（`file_role` も持たない）。
    """
    bad = sorted(
        s
        for s in sources
        if s.startswith(f"{Kind.FILE}:") and s != f"{Kind.FILE}:{CONTENT_ALLOWED_FILE_ROLE}"
    )
    if bad:
        raise DesignProvenanceError(
            f"設計ロール({role})のプロンプトに、file_role={CONTENT_ALLOWED_FILE_ROLE} 以外の"
            f"ファイルユニットの中身が入っています: {bad}。索引の作り方（reference 以外の"
            "ファイルを中身ごと索引していないか）とプロンプト構築を確認してください"
        )


def _clip(text: str | None) -> tuple[str | None, bool]:
    """保存上限で切り詰め、切り詰めたかを返す（ガード3）。"""
    if text is None:
        return None, False
    if len(text) <= MAX_FIELD_CHARS:
        return text, False
    return text[:MAX_FIELD_CHARS] + "\n…（保存上限で切り詰め）", True


async def save_design_llm_calls(
    db: AsyncSession,
    *,
    questionnaire_id: int,
    recorder: DesignLlmRecorder,
    created_by_id: int | None,
) -> int:
    """収集した来歴を保存し、保存件数を返す。

    Raises:
        DesignProvenanceError: ガード1（役割）またはガード2（内容由来テキストの出所）に
            反した。ガード2 が発火したらそのバッチ全体を保存しない。混入の疑いがある
            状態で一部だけ残すと、後から「何が保存されたか」を判断できなくなる。

    呼び出し側は原則として `save_design_llm_calls_safely` を使う（成果物を落とさない）。
    """
    if not recorder.records:
        return 0

    # 入口（recorder の生成）でも検査しているが、`recorder.role` は生成後に再代入できる
    # ので保存時にも検査する。層として独立させるため、入口の検査には依存しない。
    _assert_design_role(recorder.role)
    _guard_content_sources(recorder.content_sources, role=recorder.role)
    content_sources = sorted(recorder.content_sources)

    rows: list[DesignLlmCall] = []
    for rec in recorder.records:
        system_prompt, t1 = _clip(rec.system_prompt)
        user_message, t2 = _clip(rec.user_message)
        raw_output, t3 = _clip(rec.raw_output)
        # **thinking も同じ上限で切る**。推論本文は長くなりうるので、
        # プロンプトや生応答と同じ扱いにする。切ったことは `truncated` に載せる
        # ——「短い thinking だった」と「切られた」を混同させない。
        thinking, t4 = _clip(rec.thinking)
        rows.append(
            DesignLlmCall(
                questionnaire_id=questionnaire_id,
                role=recorder.role,
                label=rec.label,
                model=rec.model,
                provider=rec.provider,
                options=rec.options,
                system_prompt=system_prompt or "",
                user_message=user_message or "",
                raw_output=raw_output,
                error=rec.error,
                chunk_index=rec.chunk_index,
                item_count=rec.item_count,
                truncated=t1 or t2 or t3 or t4,
                content_sources=content_sources,
                index_build_info=recorder.index_build_info,
                duplicated_link_ids=recorder.duplicated_link_ids,
                model_digest=recorder.model_digest,
                model_digest_unavailable=recorder.model_digest_unavailable,
                think=rec.think,
                thinking=thinking,
                endpoint=rec.endpoint,
                timing=rec.timing,
                created_by_id=created_by_id,
            )
        )

    db.add_all(rows)
    await db.commit()
    logger.info(
        f"設計ロールの来歴を保存: role={recorder.role}, questionnaire_id={questionnaire_id}, "
        f"{len(rows)}件, content_sources={content_sources}"
    )
    return len(rows)


async def save_design_llm_calls_safely(
    db: AsyncSession,
    *,
    questionnaire_id: int,
    recorder: DesignLlmRecorder,
    created_by_id: int | None,
) -> int:
    """来歴を保存する。失敗しても例外を投げず、保存件数（失敗時は0）を返す。

    **来歴の保存に失敗しても AI 成果物は落とさない**。
    成果物は人間の確認を経て confirmed になるので、来歴の欠落が可視化されていれば
    判断できる。一方で成果物を落とすと、失敗パスでは元の AI エラー内容まで
    generic 500 に化けて操作者が原因を追えなくなる。

    ただし**「記録に失敗した」ことは必ず残す**。ここを黙って 0 件で通すと、後から
    「AI を使っていない」と「記録に失敗した」が区別できなくなる。ガード2 の発火は
    混入の疑いという最も監査に残すべき事象なので `logger.error` で、その他の保存失敗は
    `logger.warning` で残す。

    **この区別が残るのはサーバログだけである。** 確認書の開示欄は保存済み行を集計する
    ので、保存されなかった呼び出しは確認書には現れない（`docs/design_llm_provenance.md`
    の「来歴の保存に失敗したときどうするか」に同じ整理を書いた）。
    """
    try:
        return await save_design_llm_calls(
            db,
            questionnaire_id=questionnaire_id,
            recorder=recorder,
            created_by_id=created_by_id,
        )
    except DesignProvenanceError as e:
        await db.rollback()
        logger.error(
            f"設計ロールの来歴をガードにより保存しなかった: role={recorder.role}, "
            f"questionnaire_id={questionnaire_id}, {len(recorder.records)}件, 理由={e}"
        )
        return 0
    except Exception as e:  # noqa: BLE001 - 来歴の保存失敗で成果物を落とさない
        await db.rollback()
        logger.warning(
            f"設計ロールの来歴の保存に失敗した: role={recorder.role}, "
            f"questionnaire_id={questionnaire_id}, {len(recorder.records)}件, "
            f"error={type(e).__name__}: {e}"
        )
        return 0
