"""core/deps.py と core/authz.py のコア認可ロジック単体テスト。

HTTP 経由でしか触れられていなかったトークン境界・active_role 決定・患者スコープの
3分岐を、関数単体で allow/deny/境界まで固定する。
"""

import pytest
from fastapi import HTTPException
from jose import jwt

from kawasemi.core.auth import create_access_token
from kawasemi.core.authz import user_can_access_patient
from kawasemi.core.config import settings
from kawasemi.core.deps import get_current_user, get_current_user_optional, require_roles
from kawasemi.models.case_assignment import CaseAssignment
from kawasemi.models.input_target import InputTargetStatus
from kawasemi.models.role import Role, RoleType
from kawasemi.models.user import User, UserRole
from tests.lease_helpers import ensure_parents, lease_patient, make_target


def _user_with_roles(*role_names: str) -> User:
    """DB 非依存の User を役割付きで組む（test_role_switch と同一イディオム）。"""
    u = User()
    u.user_roles = [UserRole(role=Role(name=n)) for n in role_names]
    return u


# =============================================================================
# get_current_user_optional: トークン境界と active_role 決定
# =============================================================================


class TestGetCurrentUserOptional:
    @pytest.mark.asyncio
    async def test_no_token_returns_none(self, db_session) -> None:
        assert await get_current_user_optional(request=None, db=db_session, token=None) is None

    @pytest.mark.asyncio
    async def test_invalid_token_returns_none(self, db_session) -> None:
        assert (
            await get_current_user_optional(request=None, db=db_session, token="not.a.jwt") is None
        )

    @pytest.mark.asyncio
    async def test_missing_sub_returns_none(self, db_session) -> None:
        tok = create_access_token(data={})
        assert await get_current_user_optional(request=None, db=db_session, token=tok) is None

    @pytest.mark.asyncio
    async def test_non_integer_sub_returns_none(self, db_session) -> None:
        tok = create_access_token(data={"sub": "abc"})
        assert await get_current_user_optional(request=None, db=db_session, token=tok) is None

    @pytest.mark.asyncio
    async def test_nonexistent_user_returns_none(self, db_session) -> None:
        tok = create_access_token(data={"sub": "999999"})
        assert await get_current_user_optional(request=None, db=db_session, token=tok) is None

    @pytest.mark.asyncio
    async def test_forged_signature_returns_none(self, db_session, admin_user: User) -> None:
        tok = jwt.encode(
            {"sub": str(admin_user.id)}, "wrong-secret-key", algorithm=settings.jwt_algorithm
        )
        assert await get_current_user_optional(request=None, db=db_session, token=tok) is None

    @pytest.mark.asyncio
    async def test_act_claim_honored_for_switchable_role(
        self, db_session, multi_role_user: User
    ) -> None:
        tok = create_access_token(data={"sub": str(multi_role_user.id), "act": RoleType.COLLECTOR})
        user = await get_current_user_optional(request=None, db=db_session, token=tok)
        assert user is not None
        assert user.active_role == RoleType.COLLECTOR

    @pytest.mark.asyncio
    async def test_forged_act_falls_back_to_default(self, db_session, collector_user: User) -> None:
        # data_entry は admin へ切替不可 → 既定（data_entry）へフォールバックし昇格しない。
        tok = create_access_token(data={"sub": str(collector_user.id), "act": RoleType.ADMIN})
        user = await get_current_user_optional(request=None, db=db_session, token=tok)
        assert user is not None
        assert user.active_role == RoleType.COLLECTOR

    @pytest.mark.asyncio
    async def test_no_act_uses_default(self, db_session, admin_user: User) -> None:
        tok = create_access_token(data={"sub": str(admin_user.id)})
        user = await get_current_user_optional(request=None, db=db_session, token=tok)
        assert user is not None
        assert user.active_role == RoleType.ADMIN


# =============================================================================
# get_current_user: 401
# =============================================================================


class TestGetCurrentUser:
    @pytest.mark.asyncio
    async def test_none_raises_401(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await get_current_user(user=None)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_user_passthrough(self) -> None:
        u = _user_with_roles(RoleType.ADMIN)
        assert await get_current_user(user=u) is u


# =============================================================================
# require_roles: アクティブ役割による許可/拒否（DB/HTTP 非依存）
# =============================================================================


class TestRequireRoles:
    @pytest.mark.asyncio
    async def test_matching_active_role_allowed(self) -> None:
        u = _user_with_roles(RoleType.ADMIN)
        u.active_role = RoleType.ADMIN
        checker = require_roles(RoleType.ADMIN)
        assert await checker(user=u) is u

    @pytest.mark.asyncio
    async def test_mismatched_active_role_403(self) -> None:
        u = _user_with_roles(RoleType.COLLECTOR)
        u.active_role = RoleType.COLLECTOR
        checker = require_roles(RoleType.ADMIN)
        with pytest.raises(HTTPException) as exc:
            await checker(user=u)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_or_semantics_multiple_roles(self) -> None:
        checker = require_roles(RoleType.DESIGNER, RoleType.DESIGNER)
        u = _user_with_roles(RoleType.DESIGNER)
        u.active_role = RoleType.DESIGNER
        assert await checker(user=u) is u


# =============================================================================
# authz.user_can_access_patient: admin / 有効なリース / 期限切れ・完了・他者 / 拒否
# =============================================================================


class TestUserCanAccessPatient:
    @pytest.mark.asyncio
    async def test_admin_does_not_access_any_patient(self, db_session, admin_user: User) -> None:
        """管理者は患者データを見ない。以前の「admin は全患者」の抜け道は無い。"""
        admin_user.active_role = RoleType.ADMIN
        assert await user_can_access_patient(db_session, admin_user, "ANY-PATIENT") is False

    @pytest.mark.asyncio
    async def test_own_valid_lease_allowed(self, db_session, collector_user: User) -> None:
        collector_user.active_role = RoleType.COLLECTOR
        await lease_patient(db_session, collector_user.id, "P-IT")
        assert await user_can_access_patient(db_session, collector_user, "P-IT") is True

    @pytest.mark.asyncio
    async def test_expired_lease_denied(self, db_session, collector_user: User) -> None:
        collector_user.active_role = RoleType.COLLECTOR
        await lease_patient(db_session, collector_user.id, "P-EXP", minutes=-1)
        assert await user_can_access_patient(db_session, collector_user, "P-EXP") is False

    @pytest.mark.asyncio
    async def test_completed_target_denied(self, db_session, collector_user: User) -> None:
        collector_user.active_role = RoleType.COLLECTOR
        await lease_patient(
            db_session, collector_user.id, "P-DONE", status=InputTargetStatus.COMPLETED
        )
        assert await user_can_access_patient(db_session, collector_user, "P-DONE") is False

    @pytest.mark.asyncio
    async def test_started_by_without_lease_denied(self, db_session, collector_user: User) -> None:
        """旧仕様の「started_by が本人なら可」は認可にならない（期限の無い行は無効）。"""
        collector_user.active_role = RoleType.COLLECTOR
        await make_target(db_session, "P-OLD", holder_id=collector_user.id)
        assert await user_can_access_patient(db_session, collector_user, "P-OLD") is False

    @pytest.mark.asyncio
    async def test_case_assignment_no_longer_authorizes(
        self, db_session, collector_user: User
    ) -> None:
        """CaseAssignment は認可の根拠ではない（残っているのは表だけ）。"""
        collector_user.active_role = RoleType.COLLECTOR
        await ensure_parents(db_session, 1, 1)
        db_session.add(
            CaseAssignment(questionnaire_id=1, patient_id="P-CA", assigned_to_id=collector_user.id)
        )
        await db_session.commit()
        assert await user_can_access_patient(db_session, collector_user, "P-CA") is False

    @pytest.mark.asyncio
    async def test_other_users_lease_denied(self, db_session, collector_user: User) -> None:
        collector_user.active_role = RoleType.COLLECTOR
        other = User(
            username="other_holder", email="o@example.com", password_hash="x", full_name="o"
        )
        db_session.add(other)
        await db_session.flush()
        await lease_patient(db_session, other.id, "P-OTHER")
        assert await user_can_access_patient(db_session, collector_user, "P-OTHER") is False

    @pytest.mark.asyncio
    async def test_unassigned_denied(self, db_session, collector_user: User) -> None:
        collector_user.active_role = RoleType.COLLECTOR
        assert await user_can_access_patient(db_session, collector_user, "NOPE") is False
