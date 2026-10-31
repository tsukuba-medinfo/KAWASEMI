// データ所在の編集画面（テンプレートから切り出した）
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
const SOURCE_LABELS = CFG.source_labels;
const MANUAL_CATEGORIES = CFG.manual_categories;
const INIT = JSON.parse(document.getElementById('init-state').textContent || '{}');
const TOP_N = 3;  // 既定表示件数（残りは「もっと見る」で展開）
// 1項目あたりに保存できる候補の上限（サーバの `schemas.MAX_LOCATION_CANDIDATES`）。
// **サーバから渡す。** 画面側だけで持つと、上限を上げたときに 422 が出る画面になる。
const MAX_CANDIDATES = CFG.max_location_candidates;

// link_id -> 表示・選択状態
const state = {};

function starStr(n) {
  n = Math.max(0, Math.min(3, n || 0));
  return '★'.repeat(n) + '☆'.repeat(3 - n);
}

// 候補2件が同じ所在を指すかを、サーバ側の `compute_cand_key`（models.py）と同じ4フィールド
// （source_category / location / unit_id / connection_id）で判定する。復元のときに、保存
// された決着が提示スレートの何番目だったかを引き当てるために使う。
function sameCandidate(a, b) {
  if (!a || !b) return false;
  return (a.source_category || null) === (b.source_category || null)
    && (a.location || null) === (b.location || null)
    && (a.unit_id || null) === (b.unit_id || null)
    && (a.connection_id || null) === (b.connection_id || null);
}

function initState(linkId) {
  // INIT[linkId] は {candidates（人の決着）, proposed（AI が提示した全件）, confirmed}。
  // **提示された候補を丸ごと復元する**。以前は決着1件だけを返していたので、
  // 読み込み直すと残りが消えて選び直せなかった。
  const init = INIT[linkId] || {};
  const proposed = init.proposed || [];
  const saved = (init.candidates || [])[0] || null;
  const s = {
    // **検索が当てた候補と、受け皿（フェイルセーフ）を分けて持つ**。
    // 混ぜると受け皿が「もっと見る」の中へ隠れ、8枠の外に足した意味が消える。
    candidates: proposed.filter(c => !c.fallback),
    fallbacks: proposed.filter(c => c.fallback),
    selected: null,        // 数値index / 'fb<i>' / 'manual' / 'na' / null(未選択)
    // **代替候補の優先順**。`selected` に続く第2候補以降を、選んだ順の
    // キー配列で持つ。`SystemLocation.candidates` は設計上「優先順の候補配列」なのに、
    // 入力手段が単選択しか無かったので現実には常に長さ ≤1 だった。
    alternates: [],
    manual: { source_category: 'manual', location: '' },
    expanded: false,
    confirmed: !!init.confirmed,
  };
  if (saved) {
    if (saved.source_category === 'not_available') {
      s.selected = 'na';
    } else {
      const fb = s.fallbacks.findIndex(c => sameCandidate(c, saved));
      const i = fb >= 0 ? -1 : s.candidates.findIndex(c => sameCandidate(c, saved));
      if (fb >= 0) {
        s.selected = 'fb' + fb;
      } else if (i >= 0) {
        s.selected = i;
      } else if (saved.unit_id || (saved.source_category && saved.source_category !== 'manual')) {
        // 提示スレートに無い索引/種別由来の候補（提示より前に保存された行、または
        // 提示をやり直して顔ぶれが変わった行）。決着そのものは残すので先頭へ挿す。
        s.candidates = [saved].concat(s.candidates);
        s.selected = 0;
      } else {
        s.selected = 'manual';
        s.manual = { source_category: saved.source_category || 'manual', location: saved.location || '' };
      }
    }
  }
  // 保存済みの第2候補以降を復元する。提示スレートに無い候補は、主候補と
  // 同じ扱いで候補一覧へ足してから参照する（決着を落とさない）。
  (init.candidates || []).slice(1).forEach(alt => {
    if (!alt || alt.source_category === 'not_available') return;
    const fb = s.fallbacks.findIndex(c => sameCandidate(c, alt));
    if (fb >= 0) { s.alternates.push('fb' + fb); return; }
    let i = s.candidates.findIndex(c => sameCandidate(c, alt));
    if (i < 0) { s.candidates.push(alt); i = s.candidates.length - 1; }
    if (i !== s.selected) s.alternates.push(i);
  });
  state[linkId] = s;
  return s;
}

// 代替候補のキー（数値index / 'fb<i>'）から候補オブジェクトを引く。
function candByKey(s, key) {
  if (typeof key === 'number') return s.candidates[key];
  if (typeof key === 'string' && key.startsWith('fb')) return (s.fallbacks || [])[parseInt(key.slice(2), 10)];
  return null;
}

// 保存用の1候補（提示の付随情報は落とす）。
function toSaved(c) {
  return {
    source_category: c.source_category, location: c.location,
    unit_id: c.unit_id || null, connection_id: c.connection_id || null,
    payload: c.payload || null, llm_model: c.llm_model || null,
  };
}

function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderCard(card) {
  const linkId = card.dataset.linkId;
  const s = state[linkId];
  const list = card.querySelector('.cand-list');
  const cands = s.candidates;
  const showCount = s.expanded ? cands.length : Math.min(TOP_N, cands.length);
  let html = '';

  for (let i = 0; i < showCount; i++) {
    const c = cands[i];
    const sel = s.selected === i;
    const cls = 'cand-option' + (sel ? ' selected' : '') + (c.recommended ? ' recommended' : '');
    const catLabel = SOURCE_LABELS[c.source_category] || c.source_category || '';
    const detail = c.payload
      ? `<details class="cand-detail"><summary>詳細</summary><pre>${escapeHtml(JSON.stringify(c.payload, null, 2))}</pre></details>`
      : '';
    html += `<label class="${cls}">
      <input type="radio" name="cand-${escapeHtml(linkId)}" value="${i}" ${sel ? 'checked' : ''}>
      <span class="cand-body">
        <span class="cand-line1">
          <span class="cand-stars" title="適合度">${starStr(c.stars)}</span>
          <span class="cand-cat">${escapeHtml(catLabel)}</span>
          ${c.recommended ? '<span class="cand-rec-badge">AI推奨</span>' : ''}
          ${c.reach === 'body_unreachable' ? '<span class="cand-reach-badge" title="所在は特定できますが、本文が現在の取得経路へ連携されていないため、取得できません。連携は施設の判断です">本文は未連携</span>' : ''}
        </span>
        <span class="cand-loc">${escapeHtml(c.location || '（所在記述なし）')}</span>
        ${detail}
      </span>
      ${altToggle(s, i)}
    </label>`;
  }

  if (cands.length > TOP_N) {
    html += s.expanded
      ? `<button type="button" class="more-btn" data-act="collapse">▲ 閉じる</button>`
      : `<button type="button" class="more-btn" data-act="expand">▼ もっと見る（残り ${cands.length - TOP_N} 件）</button>`;
  }

  // **受け皿（フェイルセーフ）は8枠の外の別ブロック**。
  // 検索では診療記録の本文に届きにくいので、検索結果と混ぜず「当たっていない」と明示して出す。
  // 初期選択されることは無い（`recommended` が付かないので `applyDrafts` が選ばない）。
  if ((s.fallbacks || []).length) {
    html += `<div class="cand-fallback">
      <p class="cand-fallback-note">↓ ここから下は<strong>検索では当たっていません</strong>。構造化された列が無い項目（文章で記載される所見など）は、診療記録の本文を読んで判断します。</p>`;
    s.fallbacks.forEach((c, i) => {
      const v = 'fb' + i;
      const sel = s.selected === v;
      const detail = c.payload
        ? `<details class="cand-detail"><summary>詳細</summary><pre>${escapeHtml(JSON.stringify(c.payload, null, 2))}</pre></details>`
        : '';
      html += `<label class="cand-option ${sel ? 'selected' : ''}">
        <input type="radio" name="cand-${escapeHtml(linkId)}" value="${v}" ${sel ? 'checked' : ''}>
        <span class="cand-body">
          <span class="cand-line1"><span class="cand-fb-badge">診療記録</span></span>
          <span class="cand-loc">${escapeHtml(c.location || '（所在記述なし）')}</span>
          ${detail}
        </span>
        ${altToggle(s, v)}
      </label>`;
    });
    html += `</div>`;
  }

  // 固定の選択肢: 手動入力 / 該当データなし
  const manSel = s.selected === 'manual';
  const naSel = s.selected === 'na';
  let manInputs = '';
  if (manSel) {
    let opts = '';
    MANUAL_CATEGORIES.forEach(cat => {
      opts += `<option value="${cat}" ${s.manual.source_category === cat ? 'selected' : ''}>${escapeHtml(SOURCE_LABELS[cat] || cat)}</option>`;
    });
    manInputs = `<div class="manual-inputs">
      <select class="source-select" data-role="man-cat">${opts}</select>
      <textarea class="location-input" data-role="man-loc" placeholder="例: 198.51.100.3:5432 の lab_result テーブル、blast_pct カラム">${escapeHtml(s.manual.location)}</textarea>
    </div>`;
  }
  html += `<label class="cand-option ${manSel ? 'selected' : ''}">
    <input type="radio" name="cand-${escapeHtml(linkId)}" value="manual" ${manSel ? 'checked' : ''}>
    <span class="cand-body"><span class="cand-loc">手動入力（所在を自分で記述）</span></span>
  </label>${manInputs}`;
  html += `<label class="cand-option ${naSel ? 'selected' : ''}">
    <input type="radio" name="cand-${escapeHtml(linkId)}" value="na" ${naSel ? 'checked' : ''}>
    <span class="cand-body"><span class="cand-loc">該当データなし（この施設にこのデータは存在しない）</span></span>
  </label>`;

  html += altPanel(s);

  list.innerHTML = html;
  updateFlag(card);
}

// 候補行に出す「代替に追加／外す」ボタン。
//
// **主候補（`selected`）の行には出さない。** 同じ候補が第1候補と第2候補を兼ねる保存は
// 意味を持たない。`na` を選んでいる項目にも出さない（該当データが無いのに代替は無い）。
function altToggle(s, key) {
  if (s.selected === null || s.selected === 'na') return '';
  if (s.selected === key) return '';
  const rank = s.alternates.indexOf(key);
  if (rank >= 0) {
    return `<button type="button" class="alt-btn alt-btn--on" data-alt="remove" data-key="${key}">第${rank + 2}候補 ✕</button>`;
  }
  // 上限（サーバの `MAX_LOCATION_CANDIDATES`）に達していたら足せない。**押せるのに 422 に
  // なる画面にしない。**
  const full = s.alternates.length + 1 >= MAX_CANDIDATES;
  return `<button type="button" class="alt-btn" data-alt="add" data-key="${key}" ${full ? 'disabled title="候補は最大' + MAX_CANDIDATES + '件です"' : ''}>＋代替</button>`;
}

// 選んだ代替候補を優先順つきで並べ、▲▼ で順序を入れ替える。
function altPanel(s) {
  if (s.selected === null || s.selected === 'na' || !s.alternates.length) return '';
  let rows = '';
  s.alternates.forEach((key, i) => {
    const c = candByKey(s, key);
    if (!c) return;
    rows += `<li class="alt-row">
      <span class="alt-rank">第${i + 2}候補</span>
      <span class="alt-loc">${escapeHtml(c.location || '（所在記述なし）')}</span>
      <button type="button" class="alt-move" data-alt="up" data-idx="${i}" ${i === 0 ? 'disabled' : ''}>▲</button>
      <button type="button" class="alt-move" data-alt="down" data-idx="${i}" ${i === s.alternates.length - 1 ? 'disabled' : ''}>▼</button>
      <button type="button" class="alt-move" data-alt="remove" data-key="${key}">✕</button>
    </li>`;
  });
  if (!rows) return '';
  return `<div class="alt-panel">
    <p class="alt-note">代替候補（この順に試します）。第1候補が空だったときの取得手順を、ステップ2以降がこの順で書きます。</p>
    <ol class="alt-list">${rows}</ol>
  </div>`;
}

function updateFlag(card) {
  const s = state[card.dataset.linkId];
  const flag = card.querySelector('.item-flag');
  card.classList.remove('card-unresolved', 'card-na');
  const btn = card.querySelector('[data-role=confirm]');
  if (btn) btn.disabled = (s.selected === null);
  if (s.selected === null && s.unattempted) {
    // **未試行を「要確認（未選択）」と表示しない**。所在の確からしさの段階（rubric）を
    // 付けるのは人であり、その人が読むのはこの画面である。API の内訳だけ正しくしても、
    // ここで混ざれば段階分布が汚染される（臨床側で先に指摘された型）。
    flag.className = 'item-flag flag-unresolved'; flag.textContent = '未試行（AIの一推しが返っていません）';
    card.classList.add('card-unresolved');
  } else if (s.selected === null) {
    flag.className = 'item-flag flag-unresolved'; flag.textContent = '要確認（未選択）';
    card.classList.add('card-unresolved');
  } else if (!s.confirmed) {
    // **選択済みを「確定」と表示しない**。AI推奨は初期選択されるので、人が
    // 一度も触っていない項目でもここへ来る。上の枝と同じ理屈である——段階を付けるのは
    // 人であり、その人が読むのはこの画面なので、ここで混ざれば段階分布が汚染される。
    flag.className = 'item-flag flag-unresolved';
    flag.textContent = s.selected === 'na' ? '未確認（該当データなし）' : '未確認（選択済み）';
    card.classList.add('card-unresolved');
  } else if (s.selected === 'na') {
    flag.className = 'item-flag flag-na'; flag.textContent = '確定（該当データなし）';
    card.classList.add('card-na');
  } else {
    flag.className = 'item-flag flag-ok'; flag.textContent = '確定';
  }
}

function classify(linkId) {
  // 選んだ**中身**の内訳: 'rec' | 'manual' | 'na' | 'fallback' | 'unattempted' | 'unresolved'
  // 確認したかどうか（`confirmed`）はここでは見ない。別の軸として数える。
  const s = state[linkId];
  if (s.selected === 'na') return 'na';
  if (s.selected === 'manual') return 'manual';
  // **受け皿は「検索が当てた」に混ぜない**。AI 提案の適合率は検索の性能を
  // 測る指標なので、ここで混ぜると画面の内訳が測定と食い違う。
  if (typeof s.selected === 'string' && s.selected.startsWith('fb')) return 'fallback';
  if (s.selected === null && s.unattempted) return 'unattempted';  // 未試行は未選択と混ぜない
  if (s.selected === null) return 'unresolved';
  return s.candidates[s.selected] && s.candidates[s.selected].recommended ? 'rec' : 'manual';
}

function refreshStatusbar() {
  const c = { rec: 0, manual: 0, na: 0, fallback: 0, unattempted: 0, unresolved: 0 };
  let confirmed = 0, unconfirmed = 0;
  Object.keys(state).forEach(lid => {
    c[classify(lid)]++;
    const s = state[lid];
    if (s.selected === null) return;   // 未選択・未試行はどちらにも数えない
    if (s.confirmed) confirmed++; else unconfirmed++;
  });
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  set('sb-rec', c.rec); set('sb-manual', c.manual); set('sb-na', c.na); set('sb-unresolved', c.unresolved);
  set('sb-fallback', c.fallback);
  set('sb-unattempted', c.unattempted);
  set('sb-confirmed', confirmed); set('sb-unconfirmed', unconfirmed);
  const u = document.querySelector('.sb-unresolved');
  if (u) u.classList.toggle('warn', c.unresolved > 0);
  const p = document.querySelector('.sb-unattempted');
  if (p) p.classList.toggle('warn', c.unattempted > 0);
  const n = document.querySelector('.sb-unconfirmed');
  if (n) n.classList.toggle('warn', unconfirmed > 0);
}

// イベント委譲: ラジオ選択・もっと見る・手動入力
document.getElementById('items')?.addEventListener('change', e => {
  const card = e.target.closest('.item-card');
  if (!card) return;
  const s = state[card.dataset.linkId];
  if (e.target.matches('input[type=radio]')) {
    const v = e.target.value;
    s.selected = (v === 'manual' || v === 'na' || v.startsWith('fb')) ? v : parseInt(v, 10);
    // **主候補になった候補は代替から外す**。同じ所在を第1候補と第2候補に
    // 並べた保存は意味を持たない。「該当データなし」を選んだら代替も消す。
    if (s.selected === 'na') {
      s.alternates = [];
    } else {
      s.alternates = s.alternates.filter(k => k !== s.selected);
    }
    // 選び直したら未確認へ戻す。サーバも同じ規則で status を draft へ落とすので、
    // 画面だけ「確定」のまま残すと表示と DB が食い違う。
    s.confirmed = false;
    renderCard(card); refreshStatusbar();
  } else if (e.target.matches('[data-role=man-cat]')) {
    s.manual.source_category = e.target.value;
    s.confirmed = false;
    updateFlag(card); refreshStatusbar();
  }
});
document.getElementById('items')?.addEventListener('input', e => {
  if (e.target.matches('[data-role=man-loc]')) {
    const card = e.target.closest('.item-card');
    const s = state[card.dataset.linkId];
    s.manual.location = e.target.value;
    if (s.confirmed) { s.confirmed = false; updateFlag(card); refreshStatusbar(); }
  }
});
document.getElementById('items')?.addEventListener('click', e => {
  const card = e.target.closest('.item-card');
  if (!card) return;
  if (e.target.matches('.more-btn')) {
    state[card.dataset.linkId].expanded = e.target.dataset.act === 'expand';
    renderCard(card);
    return;
  }
  // 代替候補の追加・削除・並べ替え
  const act = e.target.dataset ? e.target.dataset.alt : null;
  if (!act) return;
  const s = state[card.dataset.linkId];
  const raw = e.target.dataset.key;
  const key = (raw !== undefined && !raw.startsWith('fb')) ? parseInt(raw, 10) : raw;
  if (act === 'add') {
    if (key === s.selected || s.alternates.indexOf(key) >= 0) return;
    if (s.alternates.length + 1 >= MAX_CANDIDATES) return;
    s.alternates.push(key);
  } else if (act === 'remove') {
    s.alternates = s.alternates.filter(k => k !== key);
  } else if (act === 'up' || act === 'down') {
    const i = parseInt(e.target.dataset.idx, 10);
    const j = act === 'up' ? i - 1 : i + 1;
    if (j < 0 || j >= s.alternates.length) return;
    const tmp = s.alternates[i]; s.alternates[i] = s.alternates[j]; s.alternates[j] = tmp;
  } else {
    return;
  }
  // 候補の顔ぶれ・順序を変えたら未確認へ戻す（ラジオの選び直しと同じ規則）。
  s.confirmed = false;
  renderCard(card); refreshStatusbar();
});

function collectItems() {
  const items = [];
  document.querySelectorAll('.item-card').forEach(card => {
    const linkId = card.dataset.linkId;
    const s = state[linkId];
    let candidates = [];
    if (s.selected === 'na') {
      candidates = [{ source_category: 'not_available', location: null }];
    } else if (s.selected === 'manual') {
      const loc = (s.manual.location || '').trim() || null;
      candidates = (loc || s.manual.source_category !== 'manual')
        ? [{ source_category: s.manual.source_category, location: loc }]
        : [];
    } else if (typeof s.selected === 'string' && s.selected.startsWith('fb')) {
      const c = (s.fallbacks || [])[parseInt(s.selected.slice(2), 10)];
      // **`fallback` は保存する候補へは載せない。** 保存されるのは人が決めた所在であり、
      // それがどう提示されたかは `proposed_candidates` 側が持っている（そちらの
      // `fallback` と突き合わせれば、受け皿から選んだ項目は機械的に数えられる）。
      if (c) candidates = [toSaved(c)];
    } else if (typeof s.selected === 'number') {
      const c = s.candidates[s.selected];
      if (c) candidates = [toSaved(c)];
    }
    // **代替候補を選んだ順で後ろへ足す**。`SystemLocation.candidates` は
    // 優先順の配列で、ここが配列順の正本になる。主候補が無いとき（未選択・候補が消えた）は
    // 代替だけを保存しない——第1候補の無い優先順は意味を持たない。
    if (candidates.length && s.selected !== 'na') {
      s.alternates.forEach(key => {
        const c = candByKey(s, key);
        if (c && candidates.length < MAX_CANDIDATES) candidates.push(toSaved(c));
      });
    }
    items.push({ link_id: linkId, item_text: card.querySelector('.item-text-val').value || null, candidates });
  });
  return items;
}

function setStatus(msg, ok) {
  const el = document.getElementById('status');
  el.textContent = msg; el.className = ok ? 'ok' : 'err';
}

// `unattemptedSet` は「AIの一推しが返らなかった」項目の link_id。
// **「妥当な候補なし」（LLM が chosen_cand_id=null を返した項目）とは別の状態**として表示する。
function applyDrafts(drafts, unattemptedSet) {
  let filled = 0;
  (drafts || []).forEach(d => {
    const card = document.querySelector(`.item-card[data-link-id="${CSS.escape(d.link_id)}"]`);
    if (!card) return;
    const s = state[d.link_id];
    const all = d.candidates || [];
    s.candidates = all.filter(c => !c.fallback);
    s.fallbacks = all.filter(c => c.fallback);
    s.expanded = false;
    s.unattempted = !!(unattemptedSet && unattemptedSet.has(d.link_id));
    // AI推奨（先頭 recommended）があれば初期選択。無ければ未選択（要確認）。
    s.selected = (s.candidates[0] && s.candidates[0].recommended) ? 0 : null;
    // 引き直したら未確認へ戻す。候補の顔ぶれも選択も入れ替わっているので、
    // 画面に出ているこの状態を人はまだ確認していない。
    s.confirmed = false;
    if (s.candidates.length) filled++;
    renderCard(card);
  });
  refreshStatusbar();
  return filled;
}

async function postDraft(body) {
  const resp = await fetch(`${BASE}/ai-input/questionnaires/${QID}/source-survey/system/ai-draft`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
    body: JSON.stringify(body),
  });
  return resp.json();
}

async function aiDraftAll() {
  const btn = document.getElementById('ai-btn');
  if (btn) btn.disabled = true;
  setStatus('AI候補を取得中...', true);
  try {
    const data = await postDraft({});
    if (!data.success) { setStatus(`エラー: ${data.message || 'AI候補の取得に失敗しました'}`, false); return; }
    const unattempted = new Set(data.unattempted_link_ids || []);
    const filled = applyDrafts(data.drafts, unattempted);
    const pending = data.unattempted || 0;
    let msg = `${pending ? '⚠' : '✓'} ${filled}件に候補を提示しました（確認して選択・保存してください）`;
    if (data.no_hit) msg += ` / ${data.no_hit}件は候補が見つかりませんでした`;
    // **未試行があるときは緑にしない**。従来この経路は 500 だったので、
    // 部分結果を 200 で返すようにした結果**「緑の成功」に化ける**退行を作りうる。
    if (pending) msg += ` / ${pending}件はAIの一推しが返りませんでした（未試行。もう一度実行してください）`;
    setStatus(msg, !pending);
  } catch (e) { setStatus(`通信エラー: ${e}`, false); }
  finally { if (btn) btn.disabled = false; }
}

async function aiDraftItem(btn, linkId) {
  btn.disabled = true;
  setStatus('AI候補を取得中...', true);
  try {
    const data = await postDraft({ link_id: linkId });
    if (!data.success) { setStatus(`エラー: ${data.message || 'AI候補の取得に失敗しました'}`, false); return; }
    const unattempted = new Set(data.unattempted_link_ids || []);
    const filled = applyDrafts(data.drafts, unattempted);
    if (unattempted.has(linkId)) {
      setStatus('⚠ AIの一推しが返りませんでした（未試行）。もう一度実行してください', false);
    } else {
      setStatus(filled ? '✓ 候補を提示しました（確認して選択・保存してください）'
                       : '候補が見つかりませんでした', filled > 0);
    }
  } catch (e) { setStatus(`通信エラー: ${e}`, false); }
  finally { btn.disabled = false; }
}

// 1項目だけを `confirm=true` で保存する。**項目単位に記録する**ので、
// 誰がいつその項目を確認したかが残り、1項目あたりの工数も復元できる。
async function confirmItem(btn, linkId) {
  const s = state[linkId];
  if (s.selected === null) { setStatus('候補を選んでから確認してください', false); return; }
  const item = collectItems().find(i => i.link_id === linkId);
  if (!item || !item.candidates.length) { setStatus('確認する内容がありません', false); return; }
  const card = document.querySelector(`.item-card[data-link-id="${CSS.escape(linkId)}"]`);
  btn.disabled = true;
  setStatus('確認を記録中...', true);
  try {
    const resp = await fetch(`${BASE}/ai-input/questionnaires/${QID}/source-survey/system?confirm=true`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify({ items: [item] }),
    });
    const data = await resp.json();
    if (data.success) {
      // **サーバが確認したと答えたときだけ画面を確定にする。** 先に画面を塗ると、
      // 保存が失敗した項目が「確定」に見える（記録の正本は DB 側である）。
      s.confirmed = true;
      setStatus(`✓ ${linkId} を確認済みにしました`, true);
    } else {
      setStatus(`エラー: ${data.message || '確認の記録に失敗しました'}`, false);
    }
  } catch (e) { setStatus(`通信エラー: ${e}`, false); }
  finally {
    btn.disabled = false;
    if (card) updateFlag(card);
    refreshStatusbar();
  }
}

async function saveSystem() {
  // 保存はいつでも可（下書き）。ただし未選択があることは確認サマリで明示する。
  // **一括保存は確認にならない**。`confirm` を付けないので既存の
  // 確認記録は保たれ、新しく確認済みになる項目も無い。確認は項目単位の操作である。
  const counts = { rec: 0, manual: 0, na: 0, fallback: 0, unattempted: 0, unresolved: 0 };
  let unconfirmed = 0;
  Object.keys(state).forEach(lid => {
    counts[classify(lid)]++;
    if (state[lid].selected !== null && !state[lid].confirmed) unconfirmed++;
  });
  let summary = `保存します。\n\nAI推奨そのまま: ${counts.rec}件\n変更/手動入力: ${counts.manual}件\n該当データなし: ${counts.na}件\n診療記録（受け皿）から選択: ${counts.fallback}件\n未選択: ${counts.unresolved}件\n未確認(選択済みだが「この項目を確認」を押していない): ${unconfirmed}件`;
  if (counts.unresolved > 0) {
    summary += `\n\n※ 未選択の項目が残っています。このまま保存できますが、未選択が残る間は手順書（次ステップ）の生成に進めません。`;
  }
  if (unconfirmed > 0) {
    summary += `\n\n※ この保存では確認済みになりません。確認は項目ごとに「この項目を確認」を押してください。`;
  }
  if (!window.confirm(summary + '\n\nよろしいですか？')) return;

  setStatus('保存中...', true);
  try {
    const resp = await fetch(`${BASE}/ai-input/questionnaires/${QID}/source-survey/system`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify({ items: collectItems() }),
    });
    const data = await resp.json();
    if (data.success) setStatus(`✓ ${data.saved}件を保存しました`, true);
    else setStatus(`エラー: ${data.message || '保存に失敗しました'}`, false);
  } catch (e) { setStatus(`通信エラー: ${e}`, false); }
}

// 初期描画
document.querySelectorAll('.item-card').forEach(card => {
  initState(card.dataset.linkId);
  renderCard(card);
});
refreshStatusbar();

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
jumpToItemFromHash();
