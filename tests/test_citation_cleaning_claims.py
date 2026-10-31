"""「洗浄されない」という主張が、必ず主語つきで書かれていることを機械で確かめる。

**この検査が要る理由。** 同じ主張が、レビューのたびに別の箇所で「まだ残っている」と見つかり続けた。
**件数を数える担保は成立しなかった**——数える単位（文／段落／ファイル、テストを含めるか）で
答えが変わり、食い違った。人が読む担保も、同じファイルの40行下を見落とす形で破れた。

**測る性質**: 「保存し直しても洗浄されない／主張のまま残る」と書いた**文**は、必ず同じ文の中に
**限定語**（索引に無い／実在しない／照合できなかった／アドレスで決まる 等）を持つ。
**リポジトリ全体**を走査する（対象を手書きにすると、主張が新しいファイルへ移ったときに落ちない）。

**なぜ主語が要るか**（向きの正本は `citation_metrics.citation_coverage` の docstring）。
索引に実在するアドレスの記録は、保存し直すと規則1 が payload を索引から導出するので**洗浄される**。
無条件に「洗浄されない」と書くと、人が正しいアドレスを貼った既存の典拠についての
記述が**逆になる**（結果の解釈に効く向きの誤り）。

**限界（実測して書く）**: これは**語の検査**である。限定語を1つも含まない**言い換え**
（「主張のまま残る」がその例）は、トリガー語に足すまで擦り抜ける。
文の意味を見ているわけではないので、**この検査は「群が閉じたこと」を証明しない**——
同型の主張を新しい語で書けば通る。守れるのは「いま知っている書き方での再発」だけである。

**結線の実測**（この検査自身にミューテーションを当てた）。

- 限定語を**2つとも**外す → 落ちる
- 限定語を**1つだけ**外す（同じ文に別の限定語が残る）→ **通る**。文がまだ対象を名指ししていれば
  等価変異だが、**等価とは限らない**——`file_export` の二節文で片方の節の限定語だけを落とすと、
  **もう片方の節に残った限定語が無条件の節を免責する**（「既存レコードはアドレスが索引に
  実在すれば洗浄されるが、既存レコードは規則2 が残すので洗浄されない」が通る）。
  **この検査が守るのは「同じ文に限定語が1つ以上ある」ことだけ**で、限定語が正しい限定かは見ない。
- **群の主張を新しいファイルへ書く** → 落ちる（対象を固定リストからリポジトリ全体の走査へ
  変えたので、新しいファイルへ移っても落ちる）

**この検査が守らないもの**（実測された穴。**限界として記録する**）。

1. **文の中の節を区別しない**（上記の二節文）。限定語が1つでも在れば、同じ文の別の節が
   無条件でも通る
2. **`_META` は引用と主節を区別しない**。禁じた書き方を引用する文（「…と書いていた」）を
   逃がすための除外なので、主節が無条件の文がその語を含んでいると見逃す
3. **段落を joined するので、`。` で終わらない箇条書きが1文に連結される**。片方の箇条書きの
   限定語が、もう片方を免責する（`citation_metrics` の2つの箇条書きが実際にこの形）
4. **この検査のファイル自身は対象外**（トリガー語と禁じた書き方をデータとして持つため）。
   だからここには群の主張を書かない
"""

from __future__ import annotations

import os
import pathlib
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

# **リポジトリ全体を走査する**。対象を手書きの固定リストにすると、
# 主張が新しいファイルへ移ったときに検査が落ちない——**群が育つ形そのもの**を許してしまう。
# 実測: 固定リスト（7ファイル）から全体走査へ広げても offender は 0 件だった。
_SUFFIXES = (".py", ".md", ".html")
# **`.claude` を除く**——`.claude/worktrees/` には**このリポジトリの別の作業ツリー**が
# 置かれる（`.git/info/exclude` で追跡外）。走査に入れると、他の枝や過去の版のコピーを
# 「このリポジトリの記述」として判定してしまう（実測: 古い版の文書のコピーが落ちた）。
_SKIP_DIRS = {
    ".venv",
    ".git",
    ".claude",
    "node_modules",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
}


def _target_files() -> list[str]:
    """走査の対象（**この検査のファイル自身は除く**）。

    このファイルはトリガー語と禁じた書き方を**データとして**持つので、含めると必ず自分に
    引っかかる。除外を1件に絞るために、ここには群の主張を書かない（規則の正本は
    `citation_metrics.citation_coverage` の docstring）。
    """
    repo = _REPO
    self_rel = str(pathlib.Path(__file__).resolve().relative_to(repo))
    out: list[str] = []
    for path in sorted(repo.rglob("*")):
        if path.suffix not in _SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(repo)
        if any(part in _SKIP_DIRS for part in rel.parts) or str(rel) == self_rel:
            continue
        # 版管理外の site/（施設固有の情報）と var/（E2E 実機層の run.md など実行で生まれる
        # もの）は走査しない。手元の結果が施設のファイルや LLM の出力に左右されないように。
        if rel.parts[:1] in (("site",), ("var",)):
            continue
        out.append(str(rel))
    return out


def _nested_git_markers() -> list[str]:
    """走査対象の中に現れた `.git`（別の作業ツリー／クローンの目印）を列挙する。

    `_SKIP_DIRS` を適用したうえで歩くので、`.claude/worktrees/` は入らない。
    作業ツリーの `.git` は**ファイル**（`gitdir:` 1行）、入れ子のクローンは
    **ディレクトリ**なので、両方を見る。
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(_REPO):
        rel_dir = Path(dirpath).relative_to(_REPO)
        # リポジトリ自身の `.git` は対象外。それ以外の階層に現れたものが混入である。
        if rel_dir != Path(".") and (".git" in dirnames or ".git" in filenames):
            out.append(str(rel_dir))
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if rel_dir == Path("."):
            # site/ は施設の private リポジトリを clone して置く場所なので、入れ子の
            # `.git` があって正しい。var/ は実行で生まれるもの。どちらも走査しない。
            dirnames[:] = [d for d in dirnames if d not in ("site", "var")]
    return sorted(out)


def test_the_scan_is_not_polluted_by_another_worktree() -> None:
    """走査対象に**別の作業ツリーが混入していない**こと。

    `_SKIP_DIRS` の `.claude` は「`.claude` という名前」を除く防御であって、原理
    （**このリポジトリが追跡対象外としているもの**）とは一致しない。作業ツリーを
    `<repo>/worktrees/` や `<repo>/review-tmp/` に置いた瞬間、別ツリーのコピーを
    このリポジトリの記述として判定する欠陥が同じ形で戻る——しかも **CI では緑**
    （そこに別ツリーが無い）なので素通りする。
    この検査は**混入そのもの**を見るので、ディレクトリ名に依らず落ちる。

    **`git ls-files` ＋ `--others --exclude-standard` の和で走査対象を決める案は採らなかった。**
    原理には合うが、テストが git のサブプロセスに依存し、CI・sdist 展開・git の無い環境での
    挙動を全部決める代償を払うことになる。`.git` の有無を見るだけなら外部プロセスが要らず、
    **まだ追跡されていない新規ファイル**に主語なしの主張を書くと落ちる性質も、
    走査方式を変えないので維持される。

    **守らないもの**: 別ツリーでない単なるコピー（`.git` を持たないディレクトリに古い版の
    文書を置く形）は検出しない。`.git` は「別の作業ツリー／クローン」の目印であって、
    「このリポジトリの記述ではない」ことの一般的な目印ではない。
    """
    nested = _nested_git_markers()
    assert not nested, (
        "走査対象の中に別の作業ツリー（またはクローン）がある。他の枝や過去の版のコピーを"
        "「このリポジトリの記述」として判定してしまうので、`.claude/worktrees/` の下へ移すか、"
        "`.git/info/exclude` と `_SKIP_DIRS` の両方へ追加すること:\n" + "\n".join(nested)
    )


# 主語なしで書くと**向きが逆になる**主張の書き方
_TRIGGERS = ("洗浄されない", "主張のまま残", "保存し直しても残", "何度保存し直しても")

# 同じ文にこれが在れば、対象が「索引に無いアドレスの記録」に限定されている
_LIMITS = (
    "索引に無い",
    "索引に無いアドレス",
    "実在しない",
    "照合できなかった",
    "アドレスで決まる",
    "アドレスが索引に実在",
    "実在するアドレス",
)

# **この検査そのものを説明する文**は対象外（禁じている書き方を引用するので必ず引っかかる）
_META = (
    "と書いてはいけない",
    "と書いていた",  # 過去の誤りの記録（禁じた書き方を引用する）
    "書いた**文**",
    "トリガー語",
    "無条件に「",
    "と書くと",
)


def _sentences(text: str) -> list[tuple[int, str]]:
    """段落を1つの文字列にしてから文に切る（行番号は段落の先頭）。

    **行で切ってはいけない**——日本語の文は行をまたぐので、限定語が前の行にあると
    「主語が無い」と誤検出する（この検査を書いたときに実測した）。
    """
    out: list[tuple[int, str]] = []
    paragraph: list[str] = []
    start = 1
    for lineno, line in enumerate(text.split("\n") + [""], start=1):
        if line.strip():
            if not paragraph:
                start = lineno
            paragraph.append(line.strip())
            continue
        if paragraph:
            joined = "".join(paragraph)
            for sentence in re.split(r"(?<=。)", joined):
                if sentence.strip():
                    out.append((start, sentence))
            paragraph = []
    return out


@pytest.mark.parametrize("rel", _target_files())
def test_every_cleaning_claim_names_its_subject(rel: str) -> None:
    """**「洗浄されない」と書いた文は、必ず同じ文で対象を限定している**こと。"""
    path = _REPO / rel
    assert path.exists(), rel
    offenders = [
        f"{rel}:{lineno}: {sentence.strip()[:160]}"
        for lineno, sentence in _sentences(path.read_text(encoding="utf-8"))
        if any(t in sentence for t in _TRIGGERS)
        and not any(limit in sentence for limit in _LIMITS)
        and not any(m in sentence for m in _META)
    ]
    assert not offenders, (
        "主語なしの「洗浄されない」がある（索引に実在するアドレスの記録は保存し直すと洗浄される"
        "ので、無条件に書くと向きが逆になる）:\n" + "\n".join(offenders)
    )
