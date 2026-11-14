"""DSLプログラムの可視化（Mermaid DAG 定義をサーバサイドで生成）

program（steps + return）を「ノード=op・エッジ=$id依存」の有向グラフとして
Mermaid 記法のテキストに変換する。ステップ4の実行トレース（trace）を渡すと、
各ノードに実値・通った分岐を注記した「値の系譜グラフ」を生成する。

クライアントはローカル vendor した Mermaid.js でこの定義を描画する。
"""

from __future__ import annotations

import re
from typing import Any

from kawasemi.ai_input.dsl import is_ref, ref_name
from kawasemi.ai_input.models import OpType

# ノードIDに使える文字へ正規化するための置換
_ID_SANITIZE = re.compile(r"\W+")
_REF_TOKEN = re.compile(r"\$([A-Za-z_]\w*)")

# 参照を探索する step フィールド（すべての op で実行時に消費される依存）。
# NOTE: `input` はここに含めない。`input` を実際に読むのは transform op だけで
# （executor._op_transform）、llm_extract/llm_judge 等は `input` を無視し prompt 内の
# $id 展開でのみ前段の値を受け取る。全 op 一律に input からエッジを引くと、実際には
# データが渡らない「幻エッジ」を監査図に描いてしまう。input は
# _collect_step_refs 内で transform のときだけ依存として扱う。
# NOTE: `guard` もここに含めない。guard は値ではなく実行可否を渡す制御依存であり、
# program_to_mermaid 側で点線エッジとして別に描く。
_REF_FIELDS = (
    "left",
    "right",
    "when",
    "then",
    "else_branch",
    "source",
)


# テンプレート中の {{name}} 参照（params 依存エッジの抽出用）
_PARAM_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _base_node_id(step_id: str) -> str:
    """step id をノードIDに使える文字へ正規化する（**衝突しうる**）。"""
    return "n_" + _ID_SANITIZE.sub("_", step_id or "x")


class _NodeIds:
    """プログラム内で一意なノードIDを配る。

    正規化だけでは**別の step が同じノードIDへ潰れる**。`a-b` と `a.b` はどちらも
    `n_a_b` になり、id を書かない step はどれも `n_x` になる。mermaid はそれを
    エラーにせず**1つのノードとして描く**ので、図の上で2つの step が融合し、
    設計者が確定する図が実際のプログラムと違う構造を見せる。プラン編集画面は
    step id を人が手入力する場所なので、現実に踏む。

    衝突したときだけ `__2` `__3` と連番を足す。**衝突しなければ従来と同じ ID** に
    なるので、既存の図の見た目（ノード形状・ラベル・エッジ）は変わらない。
    """

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self._used: set[str] = set()
        # 基底名ごとの「次に試す連番」。衝突のたびに2から数え直すと、同じ id の step が
        # k 個あるだけで O(k^2) になる（認証済みの1リクエストでワーカーが止まる）。
        self._next: dict[str, int] = {}
        self.per_step: list[str] = []
        self._by_step_id: dict[str, str] = {}
        self._missing: dict[str, str] = {}
        for step in steps:
            sid = str(step.get("id") or "")
            nid = self._claim(_base_node_id(sid))
            self.per_step.append(nid)
            # **`$id` 参照は最初に宣言した step へ向ける。** 同じ id の step が2つある
            # プログラムでは参照先が一意に決まらないので、図でも先頭を採る。
            if sid and sid not in self._by_step_id:
                self._by_step_id[sid] = nid

    def _claim(self, base: str) -> str:
        nid = base
        n = self._next.get(base, 2)
        if nid in self._used:
            # `_used` は増える一方なので、`n` より小さい連番はこの基底名で使用済み。
            # 別の step が `base__N` と同じ名前を明示していた場合だけ、先へ進む。
            nid = f"{base}__{n}"
            while nid in self._used:
                n += 1
                nid = f"{base}__{n}"
            self._next[base] = n + 1
        self._used.add(nid)
        return nid

    def of_ref(self, step_id: str) -> str:
        """`$id` 参照が指すノードID。

        どの step も宣言していない id（下書き途中のプログラムなど）は、mermaid が
        ラベル無しのノードとして描く。その場合も既存ノードへ紛れ込ませない。
        """
        known = self._by_step_id.get(step_id)
        if known is not None:
            return known
        if step_id not in self._missing:
            self._missing[step_id] = self._claim(_base_node_id(step_id))
        return self._missing[step_id]


def _esc(text: Any, limit: int = 60) -> str:
    """Mermaid ラベル用にエスケープする（引用符除去・改行空白化・長さ制限・山括弧中和）

    山括弧は全角へ中和する。label 組み立て側が改行用に意図的に挿入する `<br/>` と、
    output/reasoning 由来テキスト中の生の `<`（例: 「値<10」）とが Mermaid の htmlLabels
    解釈で衝突して図が壊れるのを防ぐ（安全面は Jinja2 autoescape + Mermaid strict で別途担保）。

    **Mermaid のディレクティブとエンティティを無害化する。** ラベルは設計者が書く step id /
    reasoning や、患者データ・LLM 出力に由来する trace の output / error から作られる。
    Mermaid は `%%{init: {...}}%%` を設定ディレクティブとして、ラベルの中でも読む
    （`themeCSS` で図やページの見た目を変え、CSS から外部 URL を取りに行かせられる）。
    そこで `%`（`%%` ディレクティブ・コメント）、`{` `}`（ディレクティブの本体）、`#`
    （`#lt;` など Mermaid 独自のエンティティ記法）を全角へ置き換える。字面は読めるまま、
    構文としては働かなくなる。
    """
    s = "" if text is None else str(text)
    s = (
        s.replace('"', "'")
        .replace("\n", " ")
        .replace("|", "/")
        .replace("<", "＜")
        .replace(">", "＞")
        .replace("%", "％")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("#", "＃")
        .replace(
            "`", "｀"
        )  # 先頭のバッククォートは Mermaid のマークダウン文字列として読まれ、字句エラーになる
    )
    if len(s) > limit:
        s = s[: max(1, limit - 3)] + "..."
    return s


def _collect_step_refs(step: dict[str, Any]) -> list[str]:
    """1 step が参照する他 step の id を集める（値フィールド＋prompt内の$id）"""
    refs: list[str] = []
    for field in _REF_FIELDS:
        v = step.get(field)
        if is_ref(v):
            refs.append(ref_name(v))
    # input は transform だけが実行時に消費する。他op（llm 等）は無視するため依存に含めない
    # （監査図を実データフローに忠実に保つ）。
    if step.get("op") == OpType.TRANSFORM and is_ref(step.get("input")):
        refs.append(ref_name(step["input"]))
    prompt = step.get("prompt")
    if isinstance(prompt, str):
        refs.extend(_REF_TOKEN.findall(prompt))
    # csv_extract の match: [{column, value}] の value が "$id" 参照を取り得る
    for cond in step.get("match") or []:
        if isinstance(cond, dict) and is_ref(cond.get("value")):
            refs.append(ref_name(cond["value"]))
    # 重複除去（順序維持）
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _collect_param_refs(step: dict[str, Any]) -> set[str]:
    """step のテンプレート・プロンプトが参照している {{name}} の集合を返す。"""
    names: set[str] = set()
    for field in ("sql", "fhir_query", "path_pattern", "path", "prompt"):
        value = step.get(field)
        if isinstance(value, str):
            names |= set(_PARAM_PLACEHOLDER_RE.findall(value))
    # csv_extract の match の value も params 展開の対象。ここを漏らすと
    # 「前段 step → params → 行の絞り込み」という依存が監査図から消える。同じ依存を
    # `"$id"` 直書きで表現した場合は辺が出るのに、params 経由だと出ない状態になる。
    for cond in step.get("match") or []:
        if isinstance(cond, dict) and isinstance(cond.get("value"), str):
            names |= set(_PARAM_PLACEHOLDER_RE.findall(cond["value"]))
    return names


def program_to_mermaid(
    program: dict[str, Any],
    trace: list[dict[str, Any]] | None = None,
) -> str:
    """program を Mermaid graph 定義へ変換する

    Args:
        program: {steps:[...], return:"$id"}
        trace: ステップ4の実行トレース（[{op,id,output,taken,error,skipped}, ...]）。
               与えると各ノードに実値・分岐・エラーを注記し、guard が偽で実行され
               なかった step（skipped）は破線グレーの未実行ノードとして描く。
    """
    steps = program.get("steps", [])
    if not steps:
        return 'graph TD\n  empty["（手順なし）"]'

    trace_by_id: dict[str, dict[str, Any]] = {}
    for t in trace or []:
        if t.get("id"):
            trace_by_id[t["id"]] = t

    lines = ["graph TD"]
    skipped_nodes: list[str] = []

    # **ノードIDは step ごとに一意に配る**。正規化だけだと `a-b` と `a.b`、
    # あるいは id を書かない step どうしが同じIDへ潰れ、mermaid が黙って1ノードに融合する。
    node_ids = _NodeIds(steps)

    # params の "$id" 束縛。「前段 step の出力が、後段の step の表名や
    # 検索値になる」という実在のデータフローなので、図に辺として描く。描かないと
    # 「なぜこの値が採られたか」を図から追えない（ALCOA+ 帰属性）。
    # 参照名 → その値を使う step id の集合。
    param_sources: dict[str, str] = {}
    for spec in program.get("params") or []:
        if not isinstance(spec, dict):
            continue
        pname = spec.get("name")
        pvalue = spec.get("value")
        if isinstance(pname, str) and is_ref(pvalue):
            param_sources[pname] = ref_name(pvalue)

    for step, nid in zip(steps, node_ids.per_step, strict=True):
        sid = step.get("id") or ""
        op = step.get("op")
        label_parts = [f"{OpType.LABELS.get(op, op)}", sid]

        tr = trace_by_id.get(sid)
        if tr is not None:
            if tr.get("skipped"):
                # guard が偽で実行されなかった step。実値も失敗理由も無い
                # 「設計どおりの未実行」であることを、エラー（⚠）と区別して明示する。
                # 条件式も併記して、確認書の trace 表と図を突き合わせられるようにする。
                cond = tr.get("guard")
                label_parts.append(
                    f"⏭ 未実行（guard 偽: {_esc(cond, limit=30)}）"
                    if cond
                    else "⏭ 未実行（guard 偽）"
                )
                skipped_nodes.append(nid)
            elif tr.get("error"):
                label_parts.append("⚠ " + _esc(tr["error"]))
            else:
                label_parts.append("= " + _esc(tr.get("output")))
        if step.get("needs_input"):
            label_parts.append("要入力")

        label = _esc("｜".join(p for p in label_parts if p))
        # 設計根拠（reasoning）は「なぜこの step か」の設計意図であり、実行事実
        # （= 実値 / ⚠ エラー）とは別物。監査者が根拠を実測値と誤認しないよう、
        # 実値ラベルとは別行（<br/>）に「📝 設計根拠:」で明示して注記する。
        reasoning = step.get("reasoning")
        if reasoning:
            label = f"{label}<br/>📝 設計根拠: {_esc(reasoning, limit=90)}"
        # branch はひし形、抽出系は角丸、その他は四角
        if op == OpType.BRANCH:
            lines.append(f'  {nid}{{"{label}"}}')
        elif op in (
            OpType.SQL_EXTRACT,
            OpType.FHIR_EXTRACT,
            OpType.EXTERNAL_DB_EXTRACT,
            OpType.CSV_EXTRACT,
        ):
            lines.append(f'  {nid}(["{label}"])')
        else:
            lines.append(f'  {nid}["{label}"]')

        # 依存エッジ
        for r in _collect_step_refs(step):
            redge = ""
            if op == OpType.BRANCH:
                # then/else を分かるように注記
                if is_ref(step.get("then")) and ref_name(step["then"]) == r:
                    redge = "|then|"
                elif is_ref(step.get("else_branch")) and ref_name(step["else_branch"]) == r:
                    redge = "|else|"
            lines.append(f"  {node_ids.of_ref(r)} -->{redge} {nid}")

        # guard エッジ。guard は「値が流れる依存」ではなく「実行するか否か」
        # を決める制御依存なので、点線 + |guard| でデータ依存エッジと視覚的に区別する。
        guard = step.get("guard")
        if is_ref(guard):
            lines.append(f'  {node_ids.of_ref(ref_name(guard))} -. "guard" .-> {nid}')

        # params 経由の依存エッジ。この step のテンプレートが参照して
        # いるパラメータのうち、値が前段 step 由来のものを辺にする。ラベルにパラメータ名を
        # 載せ、直接の "$id" 参照と区別できるようにする。
        for pname in sorted(_collect_param_refs(step)):
            src = param_sources.get(pname)
            if src and src != sid:
                lines.append(f'  {node_ids.of_ref(src)} -. "{_esc(pname, limit=20)}" .-> {nid}')

    # return ノード
    ret = program.get("return") or program.get("return_")
    if is_ref(ret):
        lines.append('  result["最終値 (return)"]')
        lines.append(f"  {node_ids.of_ref(ref_name(ret))} --> result")

    # 未実行ノードは薄いグレー＋破線で「通らなかった経路」として描く
    if skipped_nodes:
        lines.append(
            "  classDef skipped fill:#f5f5f5,stroke:#bdbdbd,stroke-dasharray:4 3,color:#9e9e9e"
        )
        lines.append(f"  class {','.join(skipped_nodes)} skipped")

    return "\n".join(lines)
