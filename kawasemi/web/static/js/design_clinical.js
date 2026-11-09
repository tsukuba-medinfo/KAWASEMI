// 臨床的意味の編集画面（テンプレートから切り出した）
//
// **インライン `<script>` のままでは構文検査に掛けられない。** 以前、
// このページの JS に構文エラーを注入しても全テストが緑のままだったことが実測されている
// （ページの JS が丸ごとパースエラーになり、AI 下書きも保存も確認も動かない状態）。
// 実ファイルなら `node --check` に掛けられる（CI の「JS 構文チェック」が回している）。
//
// **Jinja の値はここに書けない**ので、テンプレート側が `#page-config`（JSON）で渡す。
// URL の前置（`base_path`）はサブパス配下（リバースプロキシ）で動くために必須である。

// テンプレートが埋め込んだ設定。`base_path` はリバースプロキシのサブパス（`docs/reverse_proxy.md`）。
const CFG = JSON.parse(document.getElementById('page-config').textContent || '{}');
const BASE = CFG.base_path || '';

const QID = CFG.questionnaire_id;
const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '';
const INIT = JSON.parse(document.getElementById('init-state').textContent || '{}');

// --- 典拠エディタ -----------------------------------------------------------
// 1行 = 1典拠。unit_id（索引側のアドレス）と抜粋のみを編集し、payload の他キーと
// llm_model（AI由来の来歴）は行に保持してそのまま保存し直す（由来を失わせない）。

function citationRow(citation, opts) {
  const cite = citation || {};
  const payload = (cite.payload && typeof cite.payload === 'object') ? cite.payload : {};
  const row = document.createElement('div');
  row.className = 'cite-row';
  row.dataset.payload = JSON.stringify(payload);
  row.dataset.llmModel = cite.llm_model || '';
  // このAI下書きが挿した行の印。次の下書きで**この行だけ**を取り除くために使う
  // （人が追加した典拠と、保存済みの典拠は残す）。
  if (opts && opts.aiDraft) row.dataset.aiDraft = '1';

  const unitId = document.createElement('input');
  unitId.type = 'text';
  unitId.className = 'cite-unit-id';
  unitId.placeholder = 'file:manuals/記入の手引き.pdf#p12:340-780';
  unitId.value = cite.unit_id || '';

  const snippet = document.createElement('textarea');
  snippet.className = 'cite-snippet';
  snippet.placeholder = '該当箇所の抜粋（任意）';
  snippet.value = payload.snippet || '';

  const pick = document.createElement('button');
  pick.type = 'button';
  pick.className = 'btn btn--secondary btn--sm';
  pick.textContent = '索引から選ぶ';
  pick.title = '規約文書の索引をページ番号や語で引いて典拠を選びます（AIは使いません）';
  pick.onclick = () => togglePicker(row);

  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'btn btn--secondary btn--sm';
  del.textContent = '削除';
  del.onclick = () => row.remove();

  const meta = document.createElement('span');
  meta.className = 'cite-meta';
  const label = payload.label || '';
  const parts = [];
  if (label) parts.push(label);
  if (cite.llm_model) parts.push(`AI案: ${cite.llm_model}`);
  meta.textContent = parts.join(' / ');

  row.append(unitId, snippet, pick, del, meta, buildPicker(row));
  return row;
}

function addCitation(btn) {
  btn.closest('.cite-block').querySelector('.cite-list').appendChild(citationRow(null));
}

// --- 索引から典拠を選ぶ -----------------------------------------
// `unit_id` は索引のチャンクアドレス（`file:{パス}#p{ページ}:{開始}-{終了}`）で、収録コーパスは
// 1ページが最大7チャンクに分かれ範囲も重なる。**人が手で当てられる形ではない**ので、索引を
// 引いて選ぶ。**この経路は LLM も embedding も呼ばない**——人が典拠を探すたびにモデルが動くと、
// 人手の作業とモデルの出力の分離を運用の注意だけで守ることになる。
//
// 候補の母集合は AI 経路と同じ（`is_citable_reference_unit`。目次チャンクは出ない）。

function buildPicker(row) {
  const picker = document.createElement('div');
  picker.className = 'cite-picker';
  picker.hidden = true;
  picker.innerHTML = `
    <div class="cite-picker-form">
      <label>ページ <input type="number" min="1" class="cite-picker-page" placeholder="12"></label>
      <input type="text" class="cite-picker-q" placeholder="語で絞る（例: [5] / 発症日）">
      <button type="button" class="btn btn--secondary btn--sm cite-picker-run">検索</button>
    </div>
    <div class="cite-picker-results"></div>
    <div class="cite-picker-status"></div>`;
  picker.querySelector('.cite-picker-run').onclick = () => runPickerSearch(row);
  picker.querySelector('.cite-picker-q').onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); runPickerSearch(row); }
  };
  return picker;
}

function togglePicker(row) {
  const picker = row.querySelector('.cite-picker');
  picker.hidden = !picker.hidden;
  if (!picker.hidden) picker.querySelector('.cite-picker-q').focus();
}

function pickerHit(row, hit) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'cite-hit';
  const source = document.createElement('span');
  source.className = 'cite-hit-source';
  source.textContent = hit.source;
  const snippet = document.createElement('span');
  snippet.className = 'cite-hit-snippet';
  snippet.textContent = (hit.snippet || '').slice(0, 160);
  btn.append(source, snippet);
  btn.onclick = () => applyPickedUnit(row, hit);
  return btn;
}

function applyPickedUnit(row, hit) {
  // **payload は保存時にサーバが索引から採り直す**（`routes._verified_citation`）。ここで
  // 持たせるのは表示用の出典表記だけにして、AI下書きが挿す行と同じ形に揃える。
  row.querySelector('.cite-unit-id').value = hit.unit_id;
  row.querySelector('.cite-snippet').value = hit.snippet || '';
  row.dataset.payload = JSON.stringify({ label: hit.source });
  // **AI由来の印を2つとも落とす。** 選び直したアドレスは人が索引から選んだものなので、
  // 前の行に残っていた来歴を引き継ぐと2つの形で壊れる。
  //
  // - `llmModel` は `collectCitations` がそのまま `llm_model` として保存に載せる。人が
  //   選んだ典拠が **AI由来として記録され**、人が書いた版と自動生成版の典拠を突き合わせる比較
  //   で区別が付かなくなる。下の `meta` の差し替えで画面から
  //   「AI案: …」の表示も消えるので、残っていても人には見えない
  // - `aiDraft` は `applyDraft` が「前回のAI下書きが挿した行」を選んで消すための印である
  //   （印の付いた行だけを remove する）。残すと、**次にその項目のAI下書きを引き直した
  //   時点で、人が選んだ典拠だけが黙って消える**
  row.dataset.llmModel = '';
  delete row.dataset.aiDraft;
  const meta = row.querySelector('.cite-meta');
  if (meta) meta.textContent = hit.source;
  row.querySelector('.cite-picker').hidden = true;
  setStatus('典拠を差し込みました。保存すると索引と照合されます', true);
}

async function runPickerSearch(row) {
  const picker = row.querySelector('.cite-picker');
  const results = picker.querySelector('.cite-picker-results');
  const status = picker.querySelector('.cite-picker-status');
  const pageInput = picker.querySelector('.cite-picker-page');
  // 様式ごとの典拠の範囲で絞る（別の様式の規約を典拠にしない）
  const params = new URLSearchParams({ questionnaire_id: String(QID) });
  if (pageInput.value.trim()) params.set('page', pageInput.value.trim());
  if (picker.querySelector('.cite-picker-q').value.trim()) {
    params.set('q', picker.querySelector('.cite-picker-q').value.trim());
  }
  results.innerHTML = '';
  status.textContent = '索引を検索中...';
  try {
    const resp = await fetch(`${BASE}/ai-input/reference-units?${params.toString()}`);
    const data = await resp.json();
    if (!data.success) { status.textContent = data.message || '索引を引けませんでした'; return; }
    (data.units || []).forEach(hit => results.appendChild(pickerHit(row, hit)));
    if (Array.isArray(data.page_range)) {
      pageInput.placeholder = `${data.page_range[0]}〜${data.page_range[1]}`;
    }
    // **総数と表示件数を必ず出す。** 上限で切ったことが分からないと、人はそれを
    // 「マニュアルに該当箇所が無い」と読み違える。
    if (data.reference_scope_empty) {
      // 範囲が空の0件を「該当なし」と混ぜない（「意味が決まらなかった項目」の読み違えを作る）
      status.textContent = 'この様式には典拠にしてよい規約文書が設定されていません（site/reference_scopes.yaml）';
    } else if (!data.total) {
      status.textContent = 'この条件では候補がありません（目次は候補から除いています）';
    } else if (data.total > data.shown) {
      status.textContent = `${data.total}件中${data.shown}件を表示しています。ページや語で絞ってください`;
    } else {
      status.textContent = `${data.total}件`;
    }
  } catch (e) { status.textContent = `通信エラー: ${e}`; }
}

function renderCitations() {
  document.querySelectorAll('.item-card').forEach(card => {
    const saved = (INIT[card.dataset.linkId] || {}).citations || [];
    const list = card.querySelector('.cite-list');
    list.innerHTML = '';
    saved.forEach(c => list.appendChild(citationRow(c)));
  });
}

function collectCitations(card) {
  const citations = [];
  card.querySelectorAll('.cite-row').forEach(row => {
    let payload = {};
    try { payload = JSON.parse(row.dataset.payload || '{}') || {}; } catch (e) { payload = {}; }
    const snippet = row.querySelector('.cite-snippet').value.trim();
    if (snippet) payload.snippet = snippet; else delete payload.snippet;
    const unitId = row.querySelector('.cite-unit-id').value.trim();
    if (!unitId && Object.keys(payload).length === 0) return;  // 空行は送らない
    citations.push({
      unit_id: unitId || null,
      payload: Object.keys(payload).length ? payload : null,
      llm_model: row.dataset.llmModel || null,
    });
  });
  return citations;
}

function collectItem(card) {
  return {
    link_id: card.dataset.linkId,
    item_text: card.querySelector('.item-text-val').value || null,
    annotation: card.querySelector('.annotation-input').value.trim() || null,
    citations: collectCitations(card),
    // 記述そのものの来歴。読み込み時はサーバの値、AI下書き後は applyDraft が刻んだ値。
    llm_model: card.dataset.llmModel || null,
    // **人が「該当箇所が無い」と判断した記録**。空文字は null にして、
    // 「理由を書いた」と「書いていない」をサーバ側で取り違えないようにする。
    no_citation_reason: card.querySelector('.no-citation-reason').value.trim() || null,
  };
}

function collectItems() {
  return Array.from(document.querySelectorAll('.item-card')).map(collectItem);
}

function setStatus(msg, ok) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = ok ? 'ok' : 'err';
}

// --- AI下書き（規約文書を典拠に） -------------------------------------------
// サーバは「典拠の unit_id / 出典表記」を索引から決定的に組み立てて返す。LLM が
// 選ぶのは候補番号だけなので、ここで受け取る典拠は必ず実在するチャンクを指す。
// 典拠を選べなかった項目は no_citation=true で返り、記述も空で来る（典拠のない
// 記述は採らない方針）。

// 三状態（典拠つき / 典拠なし / 未試行）の**判定はサーバが返す `draft.state`** である。
// ここで `citation` と未試行リストを突き合わせて自分で分岐してはいけない——
// JS のテストランナーが無いので、判定をここに置くと**2つの枝の中身を入れ替える変更が
// 全テストを通る**（実測済み。`hidden` の切り替えは実行時の挙動なので、
// レンダリング結果への文字列検査では原理的に届かない）。
//
// ここに残すのは**対応表だけ**にする。`null` は「注意書きをどれも出さない」を意味する。
const NOTICE_BY_STATE = {
  with_citation: null,
  no_citation: '.cite-nocite',
  unattempted: '.cite-unattempted',
};

function applyDraft(draft, llmModel) {
  const card = document.querySelector(`.item-card[data-link-id="${CSS.escape(draft.link_id)}"]`);
  if (!card) return false;

  if (draft.annotation) {
    card.querySelector('.annotation-input').value = draft.annotation;
    // 記述の来歴を**項目そのもの**へ刻む。典拠行にだけ持たせると、人が典拠行を
    // 削除したときに「LLM が書いた記述」が確認書へ手動として開示される。
    card.dataset.llmModel = (draft.citation && draft.citation.llm_model) || llmModel || '';
  }

  const list = card.querySelector('.cite-list');
  // `state` から出す注意書きを引く。表に無い値（サーバの語彙が増えたのに画面が
  // 追いついていない）は**どれも出さない**側へ倒す——誤った注意書きを出すより黙るほうがよい。
  const shown = NOTICE_BY_STATE[draft.state] || null;
  for (const sel of ['.cite-nocite', '.cite-unattempted']) {
    const el = card.querySelector(sel);
    if (el) el.hidden = (sel !== shown);
  }

  if (draft.citation) {
    // **前回のAI下書きが挿した典拠行を、新しい行を挿す直前に取り除く。** 記述は
    // 上書きなのに典拠だけ追加していくと、2回目の下書きで「記述 Y に、前回の典拠 A も
    // 紐付いている」という虚偽の対応が保存される。無関係なページを典拠に据えた記述は
    // 典拠なしより悪い（検証者が典拠を追って初めて気づく）。人が追加した行と保存済みの
    // 行は印を持たないので残る。
    //
    // **除去をこの枝の中に置くのが要点である。** 無条件に消すと、引き直しで「典拠なし」
    // が返ったときに前回のAI典拠行だけが消えて前回の記述（差し替えは
    // `if (draft.annotation)` の中だけ）が残り、記述と典拠の対応が壊れる。枝の中に
    // 置けば「保存される典拠は現在の記述に対応する」という不変条件が両方の枝で成り立つ。
    list.querySelectorAll('.cite-row[data-ai-draft="1"]').forEach(r => r.remove());
    // 典拠は行の先頭へ差し込む（人が追加した典拠は残す）
    list.prepend(citationRow({
      unit_id: draft.citation.unit_id,
      payload: Object.assign({}, draft.citation.payload || {}, {
        snippet: draft.citation.snippet || (draft.citation.payload || {}).snippet,
        label: draft.citation.source,
      }),
      llm_model: draft.citation.llm_model,
    }, { aiDraft: true }));
  }
  return true;
}

async function postDraft(body) {
  const resp = await fetch(
    `${BASE}/ai-input/questionnaires/${QID}/source-survey/clinical/ai-draft`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify(body),
    },
  );
  return await resp.json();
}

// 一括下書きは項目数ぶんの LLM 呼び出しになり数分かかる。待たされた利用者の
// 二度押しをそのまま通すと、同じ項目に対して下書きが二重に走る（design_system.html
// と同じくボタンを止める）。
async function aiDraft(btn) {
  if (btn) btn.disabled = true;
  setStatus('規約文書を検索してAI下書きを作成中...', true);
  try {
    const data = await postDraft({});
    if (!data.success) { setStatus(`エラー: ${data.message || 'AI下書きに失敗しました'}`, false); return; }
    (data.drafts || []).forEach(d => applyDraft(d, data.llm_model));
    // 「候補が見つからなかった件数」を必ず出す。出さないと、規約文書に該当が無いのか
    // 索引が引けていないのかを画面から切り分けられない（design_system.html と同じ）。
    let msg = `✓ 典拠つき${data.with_citation}件 / 典拠なし${data.no_citation}件を差し込みました`;
    if (data.no_hit) msg += `（うち${data.no_hit}件は規約文書に候補が見つかりませんでした）`;
    // **未試行は必ず出し、成功として表示しない**。緑のチェックのまま
    // 「典拠なし37件」と出すと、人はそれを「規約文書を引いても意味が決まらなかった項目」の数として読む。
    const pendingCount = data.unattempted || 0;
    if (pendingCount) {
      msg = `⚠ 典拠つき${data.with_citation}件 / 典拠なし${data.no_citation}件を差し込みました`
        + `（${pendingCount}件は下書きが返らず未試行です。典拠なしとは別です）`;
      if (data.no_hit) msg += `（うち${data.no_hit}件は規約文書に候補が見つかりませんでした）`;
    }
    msg += data.llm_model
      ? `（モデル: ${data.llm_model}）。内容を確認して保存してください`
      : '。候補が1件も無かったためAIは実行していません';
    if (pendingCount) msg += '。もう一度「AI下書き」を実行すると未入力の項目だけが対象になります';
    setStatus(msg, pendingCount === 0);
  } catch (e) { setStatus(`通信エラー: ${e}`, false); }
  finally { if (btn) btn.disabled = false; }
}

async function aiDraftItem(btn) {
  const card = btn.closest('.item-card');
  btn.disabled = true;
  setStatus('この項目のAI下書きを作成中...', true);
  try {
    const data = await postDraft({ link_id: card.dataset.linkId });
    if (!data.success) { setStatus(`エラー: ${data.message || 'AI下書きに失敗しました'}`, false); return; }
    const draft = (data.drafts || [])[0];
    if (!draft) { setStatus('この項目の下書きは作成されませんでした', false); return; }
    // 項目単位でも未試行と典拠なしを分ける。1項目でも打ち切りは起きうる。
    // **どちらかの判定はサーバの `state` が持つ**。ここは対応表を引くだけ。
    applyDraft(draft, data.llm_model);
    const MESSAGE_BY_STATE = {
      with_citation: [`✓ 典拠つきの下書きを差し込みました（モデル: ${data.llm_model}）`, true],
      no_citation: ['規約文書に該当箇所が見つかりませんでした（典拠なし）', false],
      unattempted: [
        'この項目の下書きが返りませんでした（未試行）。典拠なしとは別です。もう一度実行してください',
        false,
      ],
    };
    const [msg, ok] = MESSAGE_BY_STATE[draft.state] || ['下書きの状態が不明です', false];
    setStatus(msg, ok);
  } catch (e) { setStatus(`通信エラー: ${e}`, false); }
  finally { btn.disabled = false; }
}

// --- 保存 -------------------------------------------------------------------

async function postSave(doConfirm, items) {
  const url = `${BASE}/ai-input/questionnaires/${QID}/source-survey/clinical`
    + (doConfirm ? '?confirm=true' : '');
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
    body: JSON.stringify({ items: items || collectItems() }),
  });
  return await resp.json();
}

async function save(doConfirm, items) {
  setStatus('保存中...', true);
  try {
    const data = await postSave(doConfirm, items);
    if (!data.success) { setStatus(`エラー: ${data.message || '保存に失敗しました'}`, false); return false; }
    const suffix = doConfirm ? `（確認済みへ昇格 ${data.confirmed}件）` : '';
    setStatus(`✓ ${data.saved}件を保存しました${suffix}。再読込します...`, true);
    setTimeout(() => location.reload(), 900);
    return true;
  } catch (e) { setStatus(`通信エラー: ${e}`, false); return false; }
}

// 確認は項目単位（他3成果物と同じ粒度）。当該項目だけを送るので、他項目の
// 確認記録（確認者・日時）には触らない。
function confirmItem(btn) {
  const card = btn.closest('.item-card');
  const item = collectItem(card);
  if (!item.annotation) { setStatus('記述が空の項目は確認できません', false); return; }
  if (!confirm(`項目「${item.link_id}」を確認済み（confirmed）にします。確認者としてあなたの名前と日時が記録されます。よろしいですか？`)) return;
  confirmOne(card, item);
}

// 再読込しない。再読込すると、他項目の未保存の編集と AI 下書きが失われる。
// 保存したのはこの項目だけなので、画面もこのカードだけを更新する（確認者・日時は
// サーバが記録した値を、次に開いたときに読む）。
async function confirmOne(card, item) {
  setStatus('保存中...', true);
  try {
    const data = await postSave(true, [item]);
    if (!data.success) { setStatus(`エラー: ${data.message || '保存に失敗しました'}`, false); return; }
    const pill = card.querySelector('.status-pill');
    if (pill) {
      pill.classList.remove('st-draft', 'st-none', 'st-na');
      pill.classList.add('st-confirmed');
      pill.textContent = '確認済み';
      pill.removeAttribute('title');
    }
    setStatus(`✓ 項目「${item.link_id}」を確認済みにしました。他の項目の編集内容はそのままです。`, true);
  } catch (e) { setStatus(`通信エラー: ${e}`, false); }
}

async function saveAndExport() {
  setStatus('保存中...', true);
  try {
    const saveData = await postSave(false);
    if (!saveData.success) { setStatus(`エラー: ${saveData.message || '保存に失敗しました'}`, false); return; }

    const expResp = await fetch(`${BASE}/ai-input/questionnaires/${QID}/source-survey/clinical/export-extension`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
    });
    const expData = await expResp.json();
    if (expData.success) setStatus(`✓ ${expData.embedded}件を保存してQuestionnaireへ反映しました`, true);
    else setStatus(`エラー: ${expData.message || 'Questionnaireへの反映に失敗しました'}`, false);
  } catch (e) { setStatus(`通信エラー: ${e}`, false); }
}

async function importExtension() {
  if (!confirm('QuestionnaireのExtensionから読み込み、現在の入力を上書きします。よろしいですか？')) return;
  setStatus('読込中...', true);
  try {
    const resp = await fetch(`${BASE}/ai-input/questionnaires/${QID}/source-survey/clinical/import-extension`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
    });
    const data = await resp.json();
    if (data.success) { setStatus(`✓ ${data.imported}件を読み込みました。再読込します...`, true); setTimeout(() => location.reload(), 900); }
    else setStatus(`エラー: ${data.message || '失敗しました'}`, false);
  } catch (e) { setStatus(`通信エラー: ${e}`, false); }
}

// キャンバスから `#item=<linkId>` で飛んできたら、その項目へスクロールして目立たせる。
// どのセルを押しても画面の先頭（①）が出ていた。
function jumpToItemFromHash() {
  const m = /^#item=(.+)$/.exec(location.hash || '');
  if (!m) return;
  let linkId;
  try { linkId = decodeURIComponent(m[1]); } catch (e) { return; }
  const card = Array.from(document.querySelectorAll('.item-card[data-link-id]'))
    .find(el => el.dataset.linkId === linkId);
  if (!card) {
    setStatus(`項目「${linkId}」はこの画面にありません。先頭から表示しています`, false);
    return;
  }
  card.classList.add('item-card--jumped');
  card.scrollIntoView({ block: 'start' });
}

renderCitations();
jumpToItemFromHash();
