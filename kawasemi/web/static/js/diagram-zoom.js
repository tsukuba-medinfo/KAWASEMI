/*
 * diagram-zoom.js — 取得フロー図（Mermaid SVG）の拡大ビューア。
 *
 * 依存なしの自己完結スクリプト。ページ内の .graph-box に描画された Mermaid SVG に
 * 「⛶ 拡大」ボタンを付与し、図またはボタンのクリックで全画面ライトボックスを開く。
 * ライトボックスではホイール / ＋ − ボタンでズーム、ドラッグでパン、Esc・背景クリック・✕
 * で閉じる。開いた瞬間に画面サイズへフィットする（小さい図は自動拡大）。
 *
 * 設計上の注意:
 *   - 開く処理は document レベルのイベント委譲で行う。Mermaid が SVG ノードを差し替え／
 *     クローンしても（その際 data-* 属性はコピーされるがイベントリスナーは失われる）影響を
 *     受けないようにするため、SVG へ直接リスナーを張らない。
 *   - クローンした SVG には元の表示寸法(px)を明示設定する。width/height を消すと一部の
 *     Mermaid 出力で寸法が 0 に潰れて「灰色背景で何も見えない」状態になるため。
 *
 * 対応テンプレ: ai_input/plan.html（#graph に動的描画）/ validation/validation.html。
 */
(function () {
  "use strict";

  var STYLE_ID = "kw-diagram-zoom-style";
  var CSS = [
    ".graph-box{position:relative;}",
    ".graph-box svg{cursor:zoom-in;}",
    ".kw-zoom-btn{position:absolute;top:.35rem;right:.45rem;z-index:5;display:inline-flex;",
    "align-items:center;gap:.25rem;padding:.16rem .5rem;font-size:.72rem;line-height:1.2;",
    "cursor:pointer;border:1px solid #ccc;border-radius:4px;background:#fff;color:#333;",
    "box-shadow:0 1px 2px rgba(0,0,0,.08);}",
    ".kw-zoom-btn:hover{background:#f0f0f0;}",
    ".kw-zoom-overlay{position:fixed;inset:0;background:rgba(15,23,42,.82);z-index:10000;",
    "display:flex;flex-direction:column;}",
    ".kw-zoom-bar{display:flex;gap:.4rem;align-items:center;padding:.45rem .7rem;",
    "background:rgba(0,0,0,.35);color:#fff;font-size:.85rem;flex:0 0 auto;}",
    ".kw-zoom-bar .kw-title{font-weight:600;margin-right:auto;}",
    ".kw-zoom-bar button{min-width:2.2rem;padding:.25rem .6rem;font-size:.9rem;cursor:pointer;",
    "border:1px solid rgba(255,255,255,.4);border-radius:4px;background:rgba(255,255,255,.12);color:#fff;}",
    ".kw-zoom-bar button:hover{background:rgba(255,255,255,.25);}",
    ".kw-zoom-bar .kw-pct{min-width:3.5rem;text-align:center;font-variant-numeric:tabular-nums;}",
    ".kw-zoom-stage{flex:1 1 auto;overflow:hidden;position:relative;cursor:grab;}",
    ".kw-zoom-stage.kw-grabbing{cursor:grabbing;}",
    ".kw-zoom-canvas{position:absolute;top:0;left:0;transform-origin:0 0;}",
    ".kw-zoom-canvas svg{display:block;}",
    ".kw-zoom-hint{padding:.3rem .7rem;background:rgba(0,0,0,.25);color:#cbd5e1;font-size:.72rem;flex:0 0 auto;}",
  ].join("");

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var el = document.createElement("style");
    el.id = STYLE_ID;
    el.textContent = CSS;
    document.head.appendChild(el);
  }

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  function mkBtn(label, titleText) {
    var b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.title = titleText || label;
    return b;
  }

  // 元 SVG の自然サイズ（px）。描画済みの実寸を優先し、取れなければ viewBox から求める。
  function naturalSize(svg) {
    var r = svg.getBoundingClientRect();
    var w = r.width;
    var h = r.height;
    if (!w || !h) {
      var vb = (svg.getAttribute("viewBox") || "").split(/[\s,]+/).map(Number);
      if (vb.length === 4 && vb[2] && vb[3]) {
        w = vb[2];
        h = vb[3];
      }
    }
    return { w: w || 600, h: h || 400 };
  }

  function openLightbox(svg) {
    var nat = naturalSize(svg);
    var natW = nat.w;
    var natH = nat.h;

    var overlay = document.createElement("div");
    overlay.className = "kw-zoom-overlay";

    var bar = document.createElement("div");
    bar.className = "kw-zoom-bar";
    var title = document.createElement("span");
    title.className = "kw-title";
    title.textContent = "取得フロー図";
    var pct = document.createElement("span");
    pct.className = "kw-pct";
    var btnOut = mkBtn("－", "縮小");
    var btnIn = mkBtn("＋", "拡大");
    var btnReset = mkBtn("リセット", "画面に合わせる");
    var btnClose = mkBtn("✕ 閉じる", "閉じる (Esc)");
    bar.appendChild(title);
    bar.appendChild(btnOut);
    bar.appendChild(pct);
    bar.appendChild(btnIn);
    bar.appendChild(btnReset);
    bar.appendChild(btnClose);

    var stage = document.createElement("div");
    stage.className = "kw-zoom-stage";
    var canvas = document.createElement("div");
    canvas.className = "kw-zoom-canvas";

    var clone = svg.cloneNode(true);
    // 注意: id は消さない。Mermaid は SVG 内の <style> を「#mermaid-xxx ...」という id 限定
    // セレクタで持つため、id を外すとノードの塗り・枠・文字色が全て失われ、黒塗りになる。
    // 実寸(px)を明示。これを消すと一部 Mermaid 出力で 0 に潰れて不可視になる。
    clone.setAttribute("width", natW);
    clone.setAttribute("height", natH);
    clone.style.width = natW + "px";
    clone.style.height = natH + "px";
    clone.style.maxWidth = "none";
    clone.style.maxHeight = "none";
    canvas.appendChild(clone);
    stage.appendChild(canvas);

    var hint = document.createElement("div");
    hint.className = "kw-zoom-hint";
    hint.textContent = "ホイールでズーム / ドラッグで移動 / Esc・背景クリックで閉じる";

    overlay.appendChild(bar);
    overlay.appendChild(stage);
    overlay.appendChild(hint);
    document.body.appendChild(overlay);

    var scale = 1;
    var panX = 0;
    var panY = 0;

    function apply() {
      canvas.style.transform =
        "translate(" + panX + "px," + panY + "px) scale(" + scale + ")";
      pct.textContent = Math.round(scale * 100) + "%";
    }

    function fit() {
      var sw = stage.clientWidth;
      var sh = stage.clientHeight;
      var fitScale = Math.min((sw * 0.92) / natW, (sh * 0.88) / natH);
      scale = clamp(fitScale, 0.1, 12);
      panX = (sw - natW * scale) / 2;
      panY = (sh - natH * scale) / 2;
      apply();
    }

    function zoomAt(factor, cx, cy) {
      var sr = stage.getBoundingClientRect();
      var ox = (cx - sr.left - panX) / scale;
      var oy = (cy - sr.top - panY) / scale;
      var next = clamp(scale * factor, 0.05, 30);
      panX -= ox * (next - scale);
      panY -= oy * (next - scale);
      scale = next;
      apply();
    }

    function zoomCenter(factor) {
      var sr = stage.getBoundingClientRect();
      zoomAt(factor, sr.left + stage.clientWidth / 2, sr.top + stage.clientHeight / 2);
    }

    function close() {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onResize);
      overlay.remove();
    }

    function onKey(e) {
      if (e.key === "Escape") close();
      else if (e.key === "+" || e.key === "=") zoomCenter(1.2);
      else if (e.key === "-" || e.key === "_") zoomCenter(1 / 1.2);
      else if (e.key === "0") fit();
    }
    function onResize() {
      fit();
    }

    btnIn.onclick = function () { zoomCenter(1.25); };
    btnOut.onclick = function () { zoomCenter(1 / 1.25); };
    btnReset.onclick = function () { fit(); };
    btnClose.onclick = close;
    overlay.addEventListener("mousedown", function (e) {
      if (e.target === overlay || e.target === stage) {
        // 背景（ステージの余白）クリックで閉じる。ドラッグ開始と区別するため mouseup 位置で判定。
        startBackdrop = { x: e.clientX, y: e.clientY, t: e.target };
      }
    });

    stage.addEventListener("wheel", function (e) {
      e.preventDefault();
      zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
    }, { passive: false });

    // --- ドラッグでパン ---
    var dragging = false;
    var startX = 0, startY = 0, startPanX = 0, startPanY = 0, moved = false;
    var startBackdrop = null;
    stage.addEventListener("mousedown", function (e) {
      dragging = true;
      moved = false;
      startX = e.clientX;
      startY = e.clientY;
      startPanX = panX;
      startPanY = panY;
      stage.classList.add("kw-grabbing");
      e.preventDefault();
    });
    window.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      panX = startPanX + (e.clientX - startX);
      panY = startPanY + (e.clientY - startY);
      if (Math.abs(e.clientX - startX) + Math.abs(e.clientY - startY) > 3) moved = true;
      apply();
    });
    window.addEventListener("mouseup", function (e) {
      if (dragging && !moved && (e.target === stage || e.target === canvas)) {
        // ステージ余白のクリック（ドラッグでない）→ 閉じる。
        close();
        return;
      }
      dragging = false;
      stage.classList.remove("kw-grabbing");
    });

    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onResize);
    fit();
  }

  // 「⛶ 拡大」ボタンを .graph-box に1つ付与する（box 単位でガード。Mermaid は box 内の
  // SVG を差し替えるが box 自体は触らないため、box にフラグを置けば二重付与を防げる）。
  function addButton(box) {
    if (!box || box.dataset.kwZoomReady) return;
    if (!box.querySelector("svg")) return; // 描画完了前はスキップ（後続スキャンで付与）
    box.dataset.kwZoomReady = "1";
    var btn = mkBtn("⛶ 拡大", "取得フロー図を全画面で拡大表示");
    btn.className = "kw-zoom-btn";
    box.appendChild(btn);
  }

  function scan(root) {
    var scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll(".graph-box").forEach(addButton);
  }

  function init() {
    injectStyle();

    // 開く処理は委譲で行う（SVG ノード差し替えに無依存）。
    document.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var btn = t.closest(".kw-zoom-btn");
      if (btn) {
        var box = btn.closest(".graph-box");
        var svg = box && box.querySelector("svg");
        if (svg) openLightbox(svg);
        return;
      }
      var clickedSvg = t.closest("svg");
      if (clickedSvg && clickedSvg.closest(".graph-box")) {
        openLightbox(clickedSvg);
      }
    });

    scan(document);
    // 動的描画（plan.html の再描画 / mermaid startOnLoad）に追随してボタンを付与する。
    var mo = new MutationObserver(function () { scan(document); });
    mo.observe(document.body, { childList: true, subtree: true });
    setTimeout(function () { scan(document); }, 600);
    setTimeout(function () { scan(document); }, 1500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
