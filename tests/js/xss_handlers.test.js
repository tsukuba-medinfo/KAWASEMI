#!/usr/bin/env node
//
// XSS 対策で書き換えたクライアント側の振る舞いを jsdom で実際に動かして確かめる。
//
// 文字列の部分一致では「配線が生きているか」「イベントの順序が正しいか」が見えない。
// 実際、データセット削除の確認ダイアログは、リスナーを base.html の送信ハンドラより後に
// 登録したため、キャンセルしても削除が走る退行を起こした（Reality Check が検出）。
// ここではテンプレートの `<script>` と静的 JS を**実物のまま**読み込んで動かす。
//
// 実行: `node tests/js/xss_handlers.test.js`（pytest の `tests/test_js_handlers.py` が呼ぶ）

"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const WEB = path.join(__dirname, "..", "..", "kawasemi", "web");
const read = (...p) => fs.readFileSync(path.join(WEB, ...p), "utf8");

const HOSTILE = `x'"><img src=x onerror=alert(1)>`;

// テンプレートの `<script>`（src なし・JSON でないもの）の本体を順に返す。
function inlineScripts(templateRel) {
  const html = read("templates", templateRel);
  const out = [];
  const re = /<script(?![^>]*\b(?:src=|type="application\/json"))[^>]*>([\s\S]*?)<\/script>/g;
  let m;
  while ((m = re.exec(html))) out.push(m[1]);
  return out;
}

// Jinja の式を、テストで固定できる値に置き換える（この3種しか使っていないことも確かめる）。
function unjinja(src) {
  const out = src
    .replace(/\{\{\s*questionnaire\.id\s*\}\}/g, "1")
    .replace(/\{\{\s*op_labels\s*\|\s*tojson\s*\}\}/g, "{}")
    .replace(/\{\{\s*base_path\(\)\s*\}\}/g, "");
  assert(!/\{\{|\{%/.test(out), `未置換の Jinja が残っている: ${out.match(/\{[{%][^}]*[}%]\}/)}`);
  return out;
}

function makeWindow(bodyHtml, { confirmResult = true } = {}) {
  const dom = new JSDOM(`<!doctype html><html><head></head><body>${bodyHtml}</body></html>`, {
    runScripts: "outside-only",
    url: "http://localhost/",
  });
  const w = dom.window;
  w.fetchCalls = [];
  // 応答は返さない（base.html は応答で document.write するため、動かしたくない）。
  w.fetch = (url, opts) => {
    w.fetchCalls.push({ url, opts });
    return new Promise(() => {});
  };
  w.confirmCalls = [];
  w.confirm = (msg) => {
    w.confirmCalls.push(msg);
    return confirmResult;
  };
  w.alert = () => {};
  return w;
}

function load(w, ...sources) {
  for (const s of sources) w.eval(s);
}

const baseScript = () => {
  // base.html の head にある CSRF 送信ハンドラ（`addEventListener('submit'` を持つ script）
  const s = inlineScripts("base.html").find((b) => b.includes("addEventListener('submit'"));
  assert(s, "base.html の送信ハンドラが見つからない");
  return s;
};
const escapeJs = () => read("static", "js", "escape.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ------------------------------------------------------------------ kwEsc

test("kwEsc は & < > \" ' を実体参照にし、null/undefined は空文字にする", () => {
  const w = makeWindow("");
  load(w, escapeJs());
  const esc = w.kwEsc;
  assert.strictEqual(esc("&"), "&amp;");
  assert.strictEqual(esc("<"), "&lt;");
  assert.strictEqual(esc(">"), "&gt;");
  assert.strictEqual(esc('"'), "&quot;");
  assert.strictEqual(esc("'"), "&#39;");
  assert.strictEqual(esc(null), "");
  assert.strictEqual(esc(undefined), "");
  assert.strictEqual(esc(0), "0");
  assert.strictEqual(esc("&lt;"), "&amp;lt;", "二重エスケープされる（元の & も変換する）");
  assert.strictEqual(
    esc(HOSTILE),
    "x&#39;&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"
  );
});

test("kwEsc の結果を属性値にも本文にも入れると、パースしても元の文字列に戻る（属性を抜けない）", () => {
  const w = makeWindow("");
  load(w, escapeJs());
  const host = w.document.createElement("div");
  host.innerHTML = `<p title="${w.kwEsc(HOSTILE)}">${w.kwEsc(HOSTILE)}</p><i title='${w.kwEsc(HOSTILE)}'></i>`;
  assert.strictEqual(host.querySelectorAll("img").length, 0);
  assert.strictEqual(host.querySelector("p").getAttribute("title"), HOSTILE);
  assert.strictEqual(host.querySelector("p").textContent, HOSTILE);
  assert.strictEqual(host.querySelector("i").getAttribute("title"), HOSTILE);
  assert.strictEqual(host.querySelector("p").attributes.length, 1);
});

// ----------------------------------------- データセット削除の確認（F12 の退行）

function datasetPage(confirmResult) {
  const w = makeWindow(
    `<form method="post" action="/datasets/1/delete" data-confirm-name="${HOSTILE.replace(/"/g, "&quot;")}">
       <button type="submit">削除</button></form>`,
    { confirmResult }
  );
  // 実ページと同じ順序: base.html（head）→ ページ末尾の extra_js
  load(w, baseScript(), ...inlineScripts("dataset_list.html"));
  return w;
}

test("データセット削除: confirm をキャンセルしたら送信（fetch）しない", () => {
  const w = datasetPage(false);
  const form = w.document.querySelector("form");
  form.requestSubmit(form.querySelector("button"));
  assert.strictEqual(w.confirmCalls.length, 1);
  assert.strictEqual(w.fetchCalls.length, 0, "キャンセルしたのに fetch が走った");
});

test("データセット削除: confirm を承諾したら /datasets/1/delete へ fetch する", () => {
  const w = datasetPage(true);
  const form = w.document.querySelector("form");
  form.requestSubmit(form.querySelector("button"));
  assert.strictEqual(w.confirmCalls.length, 1);
  assert.strictEqual(w.fetchCalls.length, 1);
  assert.ok(w.fetchCalls[0].url.endsWith("/datasets/1/delete"), w.fetchCalls[0].url);
});

test("データセット削除: 確認メッセージにデータセット名がそのまま（生の文字列として）出る", () => {
  const w = datasetPage(true);
  const form = w.document.querySelector("form");
  form.requestSubmit(form.querySelector("button"));
  assert.ok(w.confirmCalls[0].includes(HOSTILE), w.confirmCalls[0]);
});

test("data-confirm-name の無いフォームは確認なしで base のハンドラが送信する", () => {
  const w = makeWindow(`<form method="post" action="/x"><button type="submit">go</button></form>`);
  load(w, baseScript(), ...inlineScripts("dataset_list.html"));
  const form = w.document.querySelector("form");
  form.requestSubmit(form.querySelector("button"));
  assert.strictEqual(w.confirmCalls.length, 0);
  assert.strictEqual(w.fetchCalls.length, 1);
});

// ------------------------------------------------------- 委譲クリックの配線

test("autofill: AI バッジのクリックは data-link-id（敵対的な値でも生のまま）で highlightEvidence を呼ぶ", () => {
  const w = makeWindow(
    `<span class="ai-badge" data-link-id="${HOSTILE.replace(/"/g, "&quot;")}">AI</span>
     <div class="evidence-item" data-link-id="card-1"><b class="inner">x</b></div>`
  );
  load(w, escapeJs(), ...inlineScripts("ai_input/autofill.html").map(unjinja));
  const seen = [];
  w.highlightEvidence = (id) => seen.push(["highlight", id]);
  w.toggleEvidence = (id) => seen.push(["toggle", id]);
  w.document.querySelector(".ai-badge").click();
  w.document.querySelector(".evidence-item .inner").click(); // 子要素のクリックでもカードに届く
  assert.deepStrictEqual(seen, [
    ["highlight", HOSTILE],
    ["toggle", "card-1"],
  ]);
});

test("autofill: 根拠カードは敵対的な linkId でもタグを作らず data-link-id に入る", () => {
  const w = makeWindow(`<div id="evidence-container"></div>`);
  load(w, escapeJs(), ...inlineScripts("ai_input/autofill.html").map(unjinja));
  w.upsertEvidenceCard(HOSTILE, HOSTILE, { trace: [{ op: HOSTILE, id: HOSTILE, taken: HOSTILE, output: HOSTILE }], value: HOSTILE }, false);
  const c = w.document.getElementById("evidence-container");
  assert.strictEqual(c.querySelectorAll("img").length, 0, c.innerHTML);
  assert.strictEqual(c.querySelector(".evidence-item").dataset.linkId, HOSTILE);
});

function designSystemPage() {
  const card = (id) => `
    <div class="item-card" data-link-id="${id.replace(/"/g, "&quot;")}">
      <span class="item-flag"></span>
      <button type="button" data-item-act="confirm">この項目を確認</button>
      <button type="button" data-item-act="redraft">この項目を引き直す</button>
      <div class="cand-list"></div>
    </div>`;
  const w = makeWindow(
    `<script id="page-config" type="application/json">${JSON.stringify({
      questionnaire_id: 1, base_path: "", source_labels: {}, manual_categories: [], max_location_candidates: 8,
    })}</script>
     <script id="init-state" type="application/json">{}</script>
     <div id="items">${card(HOSTILE)}</div>
     <div id="status"></div>`
  );
  return w;
}

test("design_system: 確認/引き直しボタンはカードの data-link-id を渡して confirmItem/aiDraftItem を呼ぶ", () => {
  const w = designSystemPage();
  load(w, escapeJs(), read("static", "js", "design_system.js"));
  const seen = [];
  w.confirmItem = (btn, id) => seen.push(["confirm", btn.dataset.itemAct, id]);
  w.aiDraftItem = (btn, id) => seen.push(["redraft", btn.dataset.itemAct, id]);
  const [confirmBtn, redraftBtn] = w.document.querySelectorAll("[data-item-act]");
  confirmBtn.click();
  redraftBtn.click();
  assert.deepStrictEqual(seen, [
    ["confirm", "confirm", HOSTILE],
    ["redraft", "redraft", HOSTILE],
  ]);
});

test("design_system: 敵対的な linkId のカードを描画してもタグが生まれない", () => {
  const w = designSystemPage();
  load(w, escapeJs(), read("static", "js", "design_system.js"));
  assert.strictEqual(w.document.querySelectorAll("img").length, 0, w.document.body.innerHTML);
});

function questionnaireListPage() {
  const w = makeWindow(
    `<a href="#" class="is-danger" data-delete-id="7" data-delete-title="${HOSTILE.replace(/"/g, "&quot;")}">削除</a>
     <div id="deleteModal" style="display:none"><p id="deleteMessage"></p><form id="deleteForm"></form></div>
     <select id="statusFilter"></select>`
  );
  load(w, ...inlineScripts("questionnaire_list.html"));
  return w;
}

test("questionnaire_list: 削除リンクは data-delete-id / data-delete-title でモーダルを開く", () => {
  const w = questionnaireListPage();
  w.document.querySelector("a[data-delete-id]").click();
  assert.strictEqual(w.document.getElementById("deleteModal").style.display, "block");
  assert.strictEqual(w.document.getElementById("deleteForm").getAttribute("action"), "/questionnaires/7/delete");
  const msg = w.document.getElementById("deleteMessage");
  assert.ok(msg.textContent.includes(HOSTILE), msg.textContent);
  assert.strictEqual(msg.querySelectorAll("img").length, 0);
});

// --------------------------------------------------------------------- 実行

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${name}\n     ${String(e.message).split("\n").join("\n     ")}`);
  }
}
console.log(`${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
