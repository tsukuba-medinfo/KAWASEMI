"""パスワードハッシュ化・検証の対称性テスト（Stage4）

ハッシュ化側だけ72バイト切り詰めて検証側で切り詰めない非対称を防ぐ。
長い/マルチバイトのパスワードでも hash→verify が往復することを確認する。
"""

from kawasemi.core.auth import get_password_hash, verify_password


def test_password_roundtrip_basic():
    h = get_password_hash("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong password", h) is False


def test_password_roundtrip_multibyte():
    # 日本語（マルチバイト）でも往復する
    pw = "ぱすわーど秘密123"
    h = get_password_hash(pw)
    assert verify_password(pw, h) is True


def test_password_roundtrip_long_password():
    # 72バイトを超える長いパスワードでも例外を出さず往復する（対称切り詰め）
    pw = "A" * 200
    h = get_password_hash(pw)
    assert verify_password(pw, h) is True


def test_password_long_shares_72byte_prefix():
    # bcrypt の仕様上、先頭72バイトが同じなら一致する（既知の制約・対称性の確認）
    h = get_password_hash("B" * 80)
    assert verify_password("B" * 72, h) is True
