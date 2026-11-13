// HTML エスケープの共有実装（全ページで base.html から読み込む）。
// & < > " ' をすべて実体参照にするので、要素本文にも二重引用符/単一引用符で囲んだ
// 属性値にも安全に埋め込める。innerHTML に値を組み立てて入れるときは、動的な値を
// 必ずこの関数に通すこと（Questionnaire の linkId・title、DSL の step id、
// FHIR の氏名などは設計者・外部データ由来で信頼できない）。
function kwEsc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
