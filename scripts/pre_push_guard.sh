#!/usr/bin/env bash
# main への直接 push をブロックするガード。
#
# 背景: PR 起点のワークフローを守るためのクライアント側ガード（事故防止）。
#   サーバー側の branch protection の有無にかかわらず、手元での誤 push を止める。
#
# 導入: make hooks
#   （= uv run pre-commit install --hook-type pre-commit --hook-type pre-push）
#   .pre-commit-config.yaml の pre-push ステージから呼ばれる。
# 緊急回避: git push --no-verify ...
#
# 参照する push 先ブランチの取得元は2系統ある。
#   1. PRE_COMMIT_REMOTE_BRANCH: pre-commit が pre-push ステージで設定する環境変数。
#      pre-commit 自身が git の stdin を消費して from/to ref を決めるため、
#      このスクリプトの stdin には push 情報が届かない。通常はこちらを使う。
#   2. stdin: git が素の pre-push フックに渡す
#      `<local ref> <local sha> <remote ref> <remote sha>` の各行。
#      スクリプトを単体で手動検証するとき用のフォールバック。

set -u

PROTECTED="main"

deny() {
  echo "" >&2
  echo "✗ '${PROTECTED}' への直接 push はブロックされています（pre-push フック）。" >&2
  echo "  PR 経由でマージしてください:" >&2
  echo "    git switch -c <branch>" >&2
  echo "    git push -u origin <branch> && gh pr create" >&2
  echo "  どうしても直接 push する場合のみ: git push --no-verify" >&2
  echo "" >&2
  exit 1
}

if [ -n "${PRE_COMMIT_REMOTE_BRANCH:-}" ]; then
  if [ "${PRE_COMMIT_REMOTE_BRANCH}" = "refs/heads/${PROTECTED}" ]; then
    deny
  fi
  exit 0
fi

while read -r _local_ref _local_sha remote_ref _remote_sha; do
  if [ "${remote_ref:-}" = "refs/heads/${PROTECTED}" ]; then
    deny
  fi
done

exit 0
