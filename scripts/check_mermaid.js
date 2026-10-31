#!/usr/bin/env node
//
// 生成した mermaid が**構文として解釈できる**ことを検査する。
//
// `kawasemi/ai_input/viz.py` の `program_to_mermaid()` はソース文字列を組み立てるが、
// テストが見ているのは**生成された文字列の部分一致**だけだった。出力が mermaid として
// 解釈できるかは誰も見ておらず、ラベルに入る自由文（step id・op 名・`_esc` を抜けた記法）が
// 図を壊しても、テストは緑のまま画面には**何も描かれない**。インライン JS が構文検査を
// 通っていなかったのと同じクラスの穴である。
//
// この図は「監査図を実データフローに忠実に保つ」ために作ったもので、
// **設計者が見て確定する**。壊れたことに気づく仕組みが「人が画面を見る」しか無かった。
//
// 実行方法（CI が行う）:
//   uv run python scripts/emit_mermaid_fixtures.py <出力先>
//   node scripts/check_mermaid.js <出力先>
//
// **画面と同じパーサで見る。** `kawasemi/web/static/js/mermaid.min.js` は画面が読み込む
// 実物そのもので、別のパーサを持ってくると「テストは通るが画面では壊れる」差が生まれる。
// ブラウザ向けのバンドルなので DOM が要る（bare node では `document is not defined` で
// 落ちる。実測）。jsdom はそのためだけの devDependency である。

"use strict";

const fs = require("fs");
const path = require("path");

const { JSDOM } = require("jsdom");
const dom = new JSDOM("<!doctype html><html><body></body></html>");
// **`navigator` は代入では入らない**（node 22 は読み取り専用のグローバルを持つ。
// node 18 では代入できるので、ローカルだけ通って CI で落ちる差になる。実測）。
function defineGlobal(name, value) {
  Object.defineProperty(globalThis, name, { value, writable: true, configurable: true });
}
defineGlobal("window", dom.window);
defineGlobal("document", dom.window.document);
defineGlobal("navigator", dom.window.navigator);
defineGlobal("HTMLElement", dom.window.HTMLElement);

const mermaid = require(path.join(__dirname, "..", "kawasemi", "web", "static", "js", "mermaid.min.js"));

const dir = process.argv[2];
if (!dir) {
  console.error("使い方: node scripts/check_mermaid.js <mermaid ファイルのディレクトリ>");
  process.exit(2);
}

const files = fs
  .readdirSync(dir)
  .filter((f) => f.endsWith(".mmd"))
  .sort();

// **0件で静かに通さない。** 出力先を間違えても「全部通った」と読める形にしない。
if (files.length === 0) {
  console.error(`エラー: ${dir} に .mmd が1件もありません（検査が空回りしています）`);
  process.exit(1);
}

(async () => {
  const failures = [];
  for (const name of files) {
    const source = fs.readFileSync(path.join(dir, name), "utf8");
    try {
      await mermaid.parse(source);
    } catch (e) {
      const message = String((e && e.message) || e).split("\n").slice(0, 3).join(" / ");
      failures.push({ name, message });
    }
  }

  for (const f of failures) {
    console.error(`NG ${f.name}: ${f.message}`);
  }
  if (failures.length) {
    console.error(`\n${failures.length}/${files.length} 件の図が mermaid として解釈できません`);
    process.exit(1);
  }
  console.log(`OK: ${files.length} 件の図が mermaid として解釈できました`);
})();
