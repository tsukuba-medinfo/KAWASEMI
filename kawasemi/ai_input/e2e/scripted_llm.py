"""台本化した LLM（E2E の決定的層。docs/e2e_pipeline.md §3）。

`make_design_llm_client()`（設計5段階）と `DSLInterpreter._ollama_client`（実行時の
llm_extract / Vision）の代わりに差し込む。契約は本物の client と同じで、
`chat(model=, messages=, format=, options=, think=)` を持ち `.message.content` を返す。

**台本に無い入力は失敗させる。** 段階が判定できない・台本に無い link_id・患者が分からない・
本文から値が読めない、はすべて `ScriptMiss` を投げる。黙って空や既定値を返すと、
「LLM が答えなかった」と「試験の配線が壊れた」を区別できなくなる。

段階は system プロンプトの同一性で決める（各モジュールの定数そのものと比べる）。

| 段階 | system プロンプト | 応答 |
|---|---|---|
| 臨床的意味 | `clinical_drafter.DRAFT_SYSTEM_PROMPT` | 本文に `cite` を含む候補の cand_id と記述 |
| 所在ヒント | `location_hint.HINT_SYSTEM_PROMPT` | `terms` / `expected_types` |
| 所在の一推し | `resource_drafter.DRAFT_SYSTEM_PROMPT` | `choose` の文字列を全部含む候補の cand_id |
| 手順書 | `runbook.RUNBOOK_SYSTEM_PROMPT` | ステップ列 |
| DSL | `compiler.COMPILE_SYSTEM_PROMPT` | プログラム |
| Vision | （なし。`images` 付き） | 画像のハッシュ → 文字起こし |
| llm_extract | （なし） | `execution.yaml` のタスクで本文から読む |

**所在探索の順位は台本に無い。** 台本が決めるのは「提示された候補のうちどれを選ぶか」と
クエリに足す語だけで、候補に入るかどうかは索引検索（`resource_drafter.retrieve_hits`）が
決める。台本が選びたい候補が提示されていなければ null を返す（本物の LLM も選べない）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from kawasemi.ai_input.e2e import E2E_DIR, E2ESubject

SCRIPT_DIR = E2E_DIR / "script"
DESIGN_SCRIPT = SCRIPT_DIR / "design.yaml"
EXECUTION_SCRIPT = SCRIPT_DIR / "execution.yaml"

STAGE_CLINICAL = "clinical"
STAGE_HINT = "location_hint"
STAGE_LOCATION = "location"
STAGE_RUNBOOK = "runbook"
STAGE_COMPILE = "compile"
STAGE_VISION = "vision"
STAGE_EXTRACT = "llm_extract"

_LINK_RE = re.compile(r"^### linkId: (\S+)\s*$", re.MULTILINE)
_CAND_RE = re.compile(r"^\s+- cand_id=(\d+): (.*)$")
_SUBJECT_RE = re.compile(r"被験者 (E2E-\d{4})")


class ScriptMiss(AssertionError):
    """台本に無い入力が来た（段階が判定できない・項目が無い・本文から読めない）。"""


@dataclass(frozen=True)
class CallRecord:
    """台本 LLM への1回の呼び出し（試験が段階ごとの回数や選択を確かめるのに使う）。"""

    stage: str
    link_ids: tuple[str, ...]
    detail: Mapping[str, Any]


def load_design_script(path: Path = DESIGN_SCRIPT) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = yaml.safe_load(path.read_text("utf-8"))["items"]
    return items


def load_execution_script(path: Path = EXECUTION_SCRIPT) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = yaml.safe_load(path.read_text("utf-8"))["tasks"]
    return tasks


def image_digest(images: Iterable[bytes]) -> str:
    """Vision へ渡すページ画像列の同一性（sha256）。"""
    h = hashlib.sha256()
    for img in images:
        h.update(hashlib.sha256(img).digest())
    return h.hexdigest()


def _blocks(user_message: str) -> list[tuple[str, list[str]]]:
    """ユーザーメッセージを `### linkId:` ごとの (link_id, 行) へ分ける。"""
    marks = list(_LINK_RE.finditer(user_message))
    out: list[tuple[str, list[str]]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(user_message)
        out.append((m.group(1), user_message[m.end() : end].splitlines()))
    return out


def _candidates(lines: list[str]) -> list[tuple[int, str]]:
    return [(int(m.group(1)), m.group(2)) for ln in lines if (m := _CAND_RE.match(ln))]


def _response(content: str) -> Any:
    return SimpleNamespace(message=SimpleNamespace(content=content, thinking=None))


class ScriptedLLM:
    """台本どおりに答える client。呼び出しは `calls` に、台本に無かった入力は `misses` に残る。

    **`misses` は試験の側で必ず空であることを確かめる。** 製品の経路は LLM の失敗を握って
    縮退する（所在ヒントは拡張なしへ、チャンクの失敗は部分結果へ、llm_extract は値なしへ）ので、
    `ScriptMiss` を投げても試験の表には「上位8件に無い」のような別の症状でしか出ない。
    投げた事実をここに残し、症状ではなく原因で落とす。

    患者は `names`（登録番号 → 氏名）だけを受け取る。期待値を持つ `E2ESubject` は渡さない
    （台本が正解を覗ける形にしない）。
    """

    provider_name = "scripted"

    def __init__(
        self,
        names: Mapping[str, str],
        *,
        design: Mapping[str, Mapping[str, Any]] | None = None,
        execution: list[Mapping[str, Any]] | None = None,
        vision_transcripts: Mapping[str, str] | None = None,
    ) -> None:
        self._design = design if design is not None else load_design_script()
        self._tasks = execution if execution is not None else load_execution_script()
        self._names = dict(names)
        self._vision = dict(vision_transcripts or {})
        self.calls: list[CallRecord] = []
        self.misses: list[str] = []
        # 段階の判定に使う system プロンプト（import は呼び出し時。循環を避ける）
        from kawasemi.ai_input import clinical_drafter, compiler, location_hint, resource_drafter
        from kawasemi.ai_input import runbook as runbook_mod

        self._stages = {
            clinical_drafter.DRAFT_SYSTEM_PROMPT: STAGE_CLINICAL,
            location_hint.HINT_SYSTEM_PROMPT: STAGE_HINT,
            resource_drafter.DRAFT_SYSTEM_PROMPT: STAGE_LOCATION,
            runbook_mod.RUNBOOK_SYSTEM_PROMPT: STAGE_RUNBOOK,
            compiler.COMPILE_SYSTEM_PROMPT: STAGE_COMPILE,
        }

    # ------------------------------------------------------------------ 入口

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        format: Any = None,  # noqa: A002 - 本物の client と同じ引数名
        options: Any = None,
        think: Any = None,
        **_: Any,
    ) -> Any:
        """1回の応答。台本に無い入力は `misses` に残してから `ScriptMiss` を投げる。

        台本の引き損ない（キーの欠落・型の誤り）も `ScriptMiss` に揃える。素の `KeyError` の
        ままだと、どの段階・どの項目で台本が足りないかが製品側のログにしか残らない。
        """
        try:
            return self._answer(messages, format)
        except ScriptMiss as e:
            self.misses.append(str(e))
            raise
        except (KeyError, TypeError, IndexError, ValueError) as e:
            miss = ScriptMiss(f"台本の引き損ない: {type(e).__name__}: {e}")
            self.misses.append(str(miss))
            raise miss from e

    def _answer(self, messages: list[dict[str, Any]], format: Any) -> Any:  # noqa: A002
        system = next((m["content"] for m in messages if m.get("role") == "system"), None)
        user = messages[-1]
        if system is not None:
            stage = self._stages.get(system)
            if stage is None:
                raise ScriptMiss(f"台本に無い system プロンプト（先頭: {system[:40]!r}）")
            return _response(
                json.dumps(self._design_stage(stage, user["content"]), ensure_ascii=False)
            )
        if user.get("images"):
            return _response(self._vision_read(user["images"]))
        if format is not None:
            # llm_judge（構造化出力つき）。E2E の様式は判定 op を使わない
            raise ScriptMiss("台本に llm_judge の応答は無い")
        return _response(self._extract(user["content"]))

    # ------------------------------------------------------------ 設計5段階

    def _item(self, link_id: str, stage: str, section: str) -> Any:
        item = self._design.get(link_id)
        if item is None:
            raise ScriptMiss(f"{stage}: 台本に無い項目 {link_id!r}")
        if section not in item or item[section] is None:
            raise ScriptMiss(f"{stage}/{link_id}: 台本に {section!r} が無い")
        return item[section]

    def _design_stage(self, stage: str, user_message: str) -> dict[str, Any]:
        blocks = _blocks(user_message)
        if not blocks:
            raise ScriptMiss(f"{stage}: プロンプトに項目（### linkId:）が無い")
        link_ids = tuple(lid for lid, _ in blocks)
        detail: dict[str, Any] = {}
        out: dict[str, Any]
        if stage == STAGE_CLINICAL:
            drafts = []
            for lid, lines in blocks:
                spec = self._item(lid, stage, "clinical")
                hits = [cid for cid, body in _candidates(lines) if spec["cite"] in body]
                chosen = hits[0] if len(hits) == 1 else None
                if len(hits) > 1:
                    raise ScriptMiss(f"{stage}/{lid}: cite {spec['cite']!r} が候補 {hits} に当たる")
                detail[lid] = chosen
                drafts.append(
                    {
                        "link_id": lid,
                        "chosen_cand_id": chosen,
                        "annotation": spec["annotation"] if chosen is not None else None,
                    }
                )
            out = {"drafts": drafts}
        elif stage == STAGE_HINT:
            out = {
                "hints": [
                    {
                        "link_id": lid,
                        "terms": list(self._item(lid, stage, "hint").get("terms") or []),
                        "expected_types": list(
                            self._item(lid, stage, "hint").get("expected_types") or []
                        ),
                    }
                    for lid, _ in blocks
                ]
            }
        elif stage == STAGE_LOCATION:
            choices = []
            for lid, lines in blocks:
                want = list(self._item(lid, stage, "location")["choose"])
                hits = [cid for cid, body in _candidates(lines) if all(w in body for w in want)]
                if len(hits) > 1:
                    raise ScriptMiss(f"{stage}/{lid}: choose {want} が候補 {hits} に当たる")
                chosen = hits[0] if hits else None
                detail[lid] = chosen
                choices.append(
                    {"link_id": lid, "chosen_cand_id": chosen, "source_category": "local_db"}
                )
            out = {"choices": choices}
        elif stage == STAGE_RUNBOOK:
            out = {
                "runbooks": [
                    {
                        "link_id": lid,
                        "steps": [
                            {"order": i, **step}
                            for i, step in enumerate(self._item(lid, stage, "runbook"), start=1)
                        ],
                    }
                    for lid, _ in blocks
                ]
            }
        else:  # STAGE_COMPILE
            out = {
                "plans": [
                    {"link_id": lid, "program": {"params": [], **self._item(lid, stage, "program")}}
                    for lid, _ in blocks
                ]
            }
        self.calls.append(CallRecord(stage, link_ids, detail))
        return out

    # ---------------------------------------------------------------- 実行時

    def _vision_read(self, images: list[bytes]) -> str:
        digest = image_digest(images)
        text = self._vision.get(digest)
        if text is None:
            raise ScriptMiss(f"{STAGE_VISION}: 台本に無い画像（sha256={digest[:12]}…）")
        self.calls.append(CallRecord(STAGE_VISION, (), {"digest": digest}))
        return text

    def _extract(self, prompt: str) -> str:
        instruction, _, body = prompt.partition("\n===")
        tasks = [t for t in self._tasks if t["match"] in instruction]
        if len(tasks) != 1:
            names = [t["name"] for t in tasks]
            raise ScriptMiss(f"{STAGE_EXTRACT}: 指示に当たるタスクが1件でない: {names}")
        task = tasks[0]
        m_subj = _SUBJECT_RE.search(instruction)
        reg_no = m_subj.group(1) if m_subj else None
        name = self._names.get(reg_no) if reg_no else None
        if reg_no is None or name is None:
            raise ScriptMiss(f"{STAGE_EXTRACT}/{task['name']}: 指示から患者を特定できない")
        if name not in body:
            # 別の患者の本文が渡った（キー解決やパスの取り違え）。値を返してはいけない
            raise ScriptMiss(
                f"{STAGE_EXTRACT}/{task['name']}: {reg_no} の本文ではない（氏名が無い）"
            )
        m = re.search(task["pattern"], body)
        if m is None:
            raise ScriptMiss(f"{STAGE_EXTRACT}/{task['name']}: 本文から値を読めない")
        if task["format"] == "date":
            value = f"{int(m['y']):04d}-{int(m['m']):02d}-{int(m['d']):02d}"
        else:
            value = m["v"].strip()
        self.calls.append(CallRecord(STAGE_EXTRACT, (), {"task": task["name"], "subject": reg_no}))
        return value

    # ------------------------------------------------------------------ 集計

    def stage_counts(self) -> dict[str, int]:
        return dict(Counter(c.stage for c in self.calls))


def build_vision_transcripts(reports_dir: Path, subjects: list[E2ESubject]) -> dict[str, str]:
    """各患者の手書き同意書を executor と同じ関数でラスタライズし、画像 → 文字起こしを作る。

    **画像は実行のたびに作る**（フォントは環境で違うので、ハッシュを固定資産にしない）。
    文字起こしは生成器が描いた行そのもの（`sources.consent_lines`）で、完全な OCR に当たる。
    """
    from kawasemi.ai_input.e2e.sources import consent_lines
    from kawasemi.ai_input.executor import DSLInterpreter

    out: dict[str, str] = {}
    for s in subjects:
        pdf = reports_dir / s.report_key / "consent.pdf"
        out[image_digest(DSLInterpreter._render_pdf_to_png(pdf))] = "\n".join(consent_lines(s))
    return out
