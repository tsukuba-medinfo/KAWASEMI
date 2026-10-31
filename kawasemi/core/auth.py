"""認証関連モジュール"""

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import JWTError, jwt

from kawasemi.core.config import settings


def _bcrypt_bytes(password: str) -> bytes:
    """bcrypt へ渡す前処理（ハッシュ化・検証で共通）。

    bcrypt は72バイトまでしか参照しない。ハッシュ化側だけ切り詰めて検証側で
    切り詰めない非対称を避けるため、両経路でこの関数を通して同一の入力を渡す。
    """
    return password.encode("utf-8")[:72]


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """パスワードを検証する

    Args:
        plain_password: 平文パスワード
        hashed_password: ハッシュ化されたパスワード

    Returns:
        一致する場合はTrue
    """
    return bcrypt.checkpw(
        _bcrypt_bytes(plain_password),
        hashed_password.encode("utf-8") if isinstance(hashed_password, str) else hashed_password,
    )


def get_password_hash(password: str) -> str:
    """パスワードをハッシュ化する

    Args:
        password: 平文パスワード

    Returns:
        ハッシュ化されたパスワード
    """
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(_bcrypt_bytes(password), salt)
    return hashed.decode("utf-8")


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """アクセストークンを生成する

    Args:
        data: トークンに含めるデータ
        expires_delta: 有効期限（Noneの場合はデフォルト値を使用）

    Returns:
        JWTトークン
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.secret_key, algorithm=settings.jwt_algorithm)
    return encoded_jwt


def decode_access_token(token: str) -> dict[str, Any] | None:
    """アクセストークンをデコードする

    Args:
        token: JWTトークン

    Returns:
        デコードされたデータ、無効な場合はNone
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    # リフレッシュトークン（type=refresh）をアクセストークンとして流用させない。
    # これを許すと、盗まれたリフレッシュトークンを認証Cookieに入れるだけで、DBの失効
    # （revoked）を迂回して長期・最高権限でアクセスできてしまう。
    if payload.get("type") == "refresh":
        return None
    return payload


def create_refresh_token(user_id: int, jti: str, expires_delta: timedelta | None = None) -> str:
    """リフレッシュトークンを生成する。

    アクセストークンと区別するため type="refresh" を載せ、失効・ローテーション用に
    jti（一意ID）を含める。jti は呼び出し側が DB（RefreshToken）へ保存する。
    """
    if expires_delta is None:
        expires_delta = timedelta(days=settings.jwt_refresh_token_expire_days)
    expire = datetime.now(UTC) + expires_delta
    to_encode: dict[str, Any] = {
        "sub": str(user_id),
        "jti": jti,
        "type": "refresh",
        "exp": expire,
    }
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_refresh_token(token: str) -> dict[str, Any] | None:
    """リフレッシュトークンをデコードする。type=refresh でなければ None。

    署名・期限の検証のみ（失効判定は呼び出し側が jti を DB 照合して行う）。
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("type") != "refresh" or not payload.get("jti"):
        return None
    return payload
