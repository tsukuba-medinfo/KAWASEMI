"""ロールモデル定義"""

from typing import TYPE_CHECKING

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kawasemi.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from kawasemi.models.user import UserRole


class RoleType:
    """ロール種別の定数（4役割）。

    7役割（admin / collection_lead / plan_designer / data_manager / data_entry /
    verifier / data_user）は細かすぎた。設計の3役割は同じ人が同じ様式を段階ごとに
    育てる作業で、1様式を通すのに役割を3回切り替えていた。4つに畳む。

    - 管理者はシステムの運用だけを持ち、設計・収集・検証の画面に入らない（患者データを見ない）。
      **他の役割のスーパーセットではない。** 同じ人が設計もするなら設計者を明示的に付与する
    - 旧名は残さない（互換の別名を残すと、また7つに戻る）。既存 DB の行は alembic
      （`d5e6f7a8b9c0_collapse_roles_to_four`）で写す
    - `DesignRole`（`ai_input/models.py`）は設計の**段階**（来歴の種別）であって役割ではない
    """

    ADMIN = "admin"  # 管理者（アカウント・役割の付与、接続と索引の状態、様式の取り込み）
    DESIGNER = "designer"  # 設計者（臨床的意味・所在・手順書・取得プラン・キャンバス・様式の内容）
    COLLECTOR = "collector"  # 収集者（データセット・割り当て・自動収集の実行・手動入力・回答送信）
    REVIEWER = (
        "reviewer"  # 閲覧・検証者（回答の閲覧・チェック・差し戻し・エクスポート・メトリクス）
    )

    ALL = [ADMIN, DESIGNER, COLLECTOR, REVIEWER]

    # 役割の優先順位（先頭が既定）。複数役割を持つユーザーの既定アクティブ役割や、
    # 切替UIの並び順に使う。管理者が先頭なのは「運用の画面から始める」ためで、上位権限の意味ではない。
    PRIORITY = [ADMIN, DESIGNER, COLLECTOR, REVIEWER]

    # UI 表示名（バッジ・役割切替・ホーム見出しで統一）。
    DISPLAY_NAMES = {
        ADMIN: "管理者",
        DESIGNER: "設計者",
        COLLECTOR: "収集者",
        REVIEWER: "閲覧・検証者",
    }

    DESCRIPTIONS = {
        ADMIN: "管理者（アカウント・役割の付与、接続と索引の状態、ログ、様式のファイル取り込み。患者データは見ない）",
        DESIGNER: "設計者（臨床的意味・所在・手順書・取得プラン・設計キャンバス・様式の内容と ValueSet の編集。回答の入力・送信はしない）",
        COLLECTOR: "収集者（データセット・症例の割り当て・自動収集のバッチ起動と本番実行・手動入力・回答送信。設計の確定はしない）",
        REVIEWER: "閲覧・検証者（回答の閲覧・チェック・検証・差し戻し・エクスポート・取得メトリクス。入力・設計はしない）",
    }

    @classmethod
    def display_name(cls, role_name: str) -> str:
        """役割名の日本語表示名を返す（未知なら role_name をそのまま）"""
        return cls.DISPLAY_NAMES.get(role_name, role_name)


class Role(Base, TimestampMixin):
    """ロールモデル"""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # リレーション
    user_roles: Mapped[list["UserRole"]] = relationship(
        "UserRole", back_populates="role", cascade="all, delete-orphan"
    )
