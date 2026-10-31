"""状態定数クラスの単体テスト。DB 不要の純粋検証。

ALL リストと LABELS/DISPLAY_NAMES/DESCRIPTIONS の網羅・整合を固定し、片方だけ
更新される回帰を検知する。
"""

from kawasemi.ai_input.executor import _DATA_OPS, _JUDGMENT_OPS
from kawasemi.ai_input.models import FailureCode, OpType
from kawasemi.models.auth_event import AuthEventAction
from kawasemi.models.batch_job import BatchJobStatus
from kawasemi.models.case_assignment import AssignmentStatus
from kawasemi.models.input_target import InputTargetStatus
from kawasemi.models.questionnaire_response import VerificationAction, VerificationStatus
from kawasemi.models.role import RoleType


def test_role_type_all_and_priority_consistent() -> None:
    assert len(RoleType.ALL) == 4
    assert set(RoleType.ALL) == set(RoleType.PRIORITY)  # 片方だけ更新の番人
    assert len(RoleType.PRIORITY) == len(set(RoleType.PRIORITY))  # 重複なし
    assert set([RoleType.DESIGNER]) <= set(RoleType.ALL)
    assert RoleType.ADMIN in RoleType.ALL


def test_role_type_display_and_descriptions_cover_all() -> None:
    assert set(RoleType.DISPLAY_NAMES) == set(RoleType.ALL)
    assert set(RoleType.DESCRIPTIONS) == set(RoleType.ALL)


def test_role_type_display_name_known_and_unknown() -> None:
    assert RoleType.display_name(RoleType.ADMIN) == RoleType.DISPLAY_NAMES[RoleType.ADMIN]
    assert RoleType.display_name("no_such_role") == "no_such_role"  # 未知はそのまま


def test_assignment_status_labels_cover_all() -> None:
    assert set(AssignmentStatus.ALL) == set(AssignmentStatus.LABELS)
    assert AssignmentStatus.ALL == ["pending", "in_progress", "completed", "cancelled"]


def test_batch_job_status_labels_cover_all() -> None:
    assert set(BatchJobStatus.ALL) == set(BatchJobStatus.LABELS)
    assert BatchJobStatus.RUNNING == "running"


def test_input_target_status_labels_cover_all() -> None:
    assert set(InputTargetStatus.ALL) == set(InputTargetStatus.LABELS)
    assert "ai_drafted" in InputTargetStatus.ALL


def test_verification_status_label_and_all() -> None:
    assert set(VerificationStatus.ALL) == set(VerificationStatus.LABELS)
    assert VerificationStatus.label(VerificationStatus.VERIFIED) == "検証済み"
    # None / 未知は空文字 or そのまま（例外を投げない）
    assert VerificationStatus.label(None) == ""
    assert VerificationStatus.label("weird") == "weird"


def test_failure_code_labels_cover_all() -> None:
    """`FailureCode` の `ALL` と `LABELS` が一致すること。

    **この整合を見るテストが無かった。** `LABELS` に足し忘れると Jinja の `Undefined` が
    空文字で描画されるので、取得困難度の画面に**ラベルの無いバッジ**（件数だけ）が出る。
    例外にならないので気づけない。
    """
    assert set(FailureCode.ALL) == set(FailureCode.LABELS)
    assert len(FailureCode.ALL) == len(set(FailureCode.ALL)), "値の重複"
    # **`not_applicable` は取得の失敗ではない**（分母から除く）。語彙に在ることを固定する
    assert FailureCode.NOT_APPLICABLE in FailureCode.ALL
    # **既存コードの相対順序が変わらないこと**（CSV のヘッダとセルがこの順序を使うので、途中に
    # 挿入すると既存 CSV との列の対応が黙って崩れる）。末尾を固定すると、コードを1つ足すたびに
    # 測定と無関係な編集を強いることになるので、プレフィックスの一致で見る
    assert FailureCode.ALL[:8] == [
        FailureCode.SOURCE_UNAVAILABLE,
        FailureCode.KEY_RESOLUTION_FAILED,
        FailureCode.NOT_FOUND_FOR_PATIENT,
        FailureCode.EXTRACTION_LOW_CONFIDENCE,
        FailureCode.TIMEOUT,
        FailureCode.SCHEMA_MISMATCH,
        FailureCode.NEEDS_INPUT,
        FailureCode.UNCLASSIFIED,
    ]


def test_judgment_and_data_op_vocabularies_are_exactly_these() -> None:
    """`not_applicable` の判定基準になる2つの op 集合を**値そのもので固定する**。

    集計テストのフィクスチャは実際に置いた op しか触らないので、**集合に op を足しても挙動
    テストは通る**（`_JUDGMENT_OPS` に `branch` を足す形が素通りした）。除外語彙に語彙テストを置いたのと
    同じ理屈をここにも適用する。

    - `_JUDGMENT_OPS` に取得でも判定でもない op（`branch` / `transform` / `select` /
      `aggregate`）を足すと、**判定が動いていない項目まで分母から外れる**（困難度を過小に）
    - `_DATA_OPS` に判定 op を足すと「取得系が全て skip」の意味が変わる
    """
    assert _JUDGMENT_OPS == {OpType.COMPARE, OpType.LLM_JUDGE}
    assert _DATA_OPS == {
        OpType.SQL_EXTRACT,
        OpType.EXTERNAL_DB_EXTRACT,
        OpType.FILE_EXTRACT,
        OpType.CSV_EXTRACT,
        OpType.FHIR_EXTRACT,
    }
    # 2つの集合は交わらない（取得しながら判定する op は無い）
    assert not (_JUDGMENT_OPS & _DATA_OPS)
    # 値を返すだけの op は判定ではない
    for op in (OpType.BRANCH, OpType.TRANSFORM, OpType.SELECT, OpType.AGGREGATE):
        assert op not in _JUDGMENT_OPS, f"{op} は条件を評価しない"


def test_verification_action_values() -> None:
    assert VerificationAction.ALL == ["verified", "rejected", "resubmitted"]


def test_auth_event_action_values() -> None:
    assert AuthEventAction.ALL == ["locked", "unlocked"]
