#!/usr/bin/env python3
"""SECRET_KEY生成スクリプト

安全なランダム文字列を生成してSECRET_KEYとして使用します。

使用方法:
    python scripts/generate_secret_key.py

または:
    python -m scripts.generate_secret_key
"""

import secrets


def generate_secret_key(length: int = 32) -> str:
    """
    暗号学的に安全なランダム文字列を生成

    Args:
        length: 生成する文字列の長さ（デフォルト: 32）

    Returns:
        str: Base64エンコードされたランダム文字列
    """
    return secrets.token_urlsafe(length)


def main() -> None:
    """メイン処理"""
    print("=" * 70)
    print("KAWASEMI SECRET_KEY 生成ツール")
    print("=" * 70)
    print()

    # SECRET_KEYの生成
    secret_key = generate_secret_key(32)

    print("✓ 安全なSECRET_KEYを生成しました:")
    print()
    print(f"  {secret_key}")
    print()
    print("=" * 70)
    print("次のステップ:")
    print("=" * 70)
    print()
    print("1. .envファイルに以下を設定してください:")
    print()
    print(f'   SECRET_KEY="{secret_key}"')
    print()
    print("2. 本番環境では、このキーを安全に管理してください:")
    print("   - バージョン管理システムにコミットしない")
    print("   - 環境変数管理ツール（AWS Secrets Manager等）の使用を推奨")
    print("   - 定期的なキーローテーションを検討")
    print()
    print("注意: このキーは一度しか表示されません。")
    print("      必ずコピーして安全な場所に保存してください。")
    print()


if __name__ == "__main__":
    main()
