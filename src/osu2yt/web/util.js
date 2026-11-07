// DOM + formatting helpers. All text goes through textContent (never innerHTML with data).

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'value') el.value = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// Like el.replaceChildren(), but skips null/false and flattens arrays (native replaceChildren prints "null").
export function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter(c => c != null && c !== false)
    .map(c => (c instanceof Node ? c : String(c))));
  return el;
}

export function fmtDur(sec) {
  if (sec == null) return '—';
  const t = Math.round(sec);
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
}

export function fmtDiff(video, song) {
  if (video == null || song == null) return null;
  const d = Math.round(video - song);
  const cls = Math.abs(d) <= 3 ? 'good' : Math.abs(d) <= 10 ? 'warn' : 'bad';
  return { text: (d > 0 ? '+' : '') + d + 's', cls };
}

export function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export const STATE_LABEL = {
  unmatched: '未マッチ',
  needs_review: '要確認',
  confirmed: '確定',
  added: '追加済み',
  skipped: 'スキップ',
  gone: '消えた',
};

export const REASON_LABEL = {
  low_score: 'スコア低',
  close_second: '別音源と僅差',
  artist_mismatch: 'アーティスト不一致',
  title_mismatch: '曲名不一致',
  no_results: '候補なし',
  pack: 'パック譜面',
  unavailable: '再生不可',
};

export function reasonLabel(reason) {
  if (!reason) return '';
  if (reason.startsWith('unavailable:')) {
    const kind = { deleted: '削除済み', private: '非公開', region_blocked: '地域制限' }[reason.slice(12)] || reason.slice(12);
    return `再生不可 (${kind})`;
  }
  return REASON_LABEL[reason] || reason;
}

export const NOTE_LABEL = {
  topic: 'Topic',
  official_artist: '公式 (本人)',
  artist_channel: '本人チャンネル',
  topic_other: 'Topic (別名義)',
  verified: '認証済み',
  official_name: 'official',
  full_size_mode: 'フル尺優先',
  short_video: '短縮版',
};

export function stateBadge(state, reason) {
  return h('span', { class: `badge st-${state}`, title: reason ? reasonLabel(reason) : null },
    STATE_LABEL[state] || state);
}

export function toast(message, kind = 'info', ms = 4000) {
  const box = document.getElementById('toasts');
  const el = h('div', { class: `toast ${kind}` }, message);
  box.append(el);
  setTimeout(() => el.classList.add('out'), ms);
  setTimeout(() => el.remove(), ms + 400);
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function osuUrl(onlineId) {
  return onlineId ? `https://osu.ppy.sh/beatmapsets/${onlineId}` : null;
}

export function ytUrl(id) {
  return `https://www.youtube.com/watch?v=${encodeURIComponent(id)}`;
}

// UI preferences (localStorage) and per-tab state (sessionStorage). Storage can throw or be empty
// (private mode, blocked site data), so every access is guarded and falls back to the default.
function store(kind) {
  return {
    get(k, d = null) {
      try { const v = window[kind].getItem('osu2yt.' + k); return v == null ? d : JSON.parse(v); } catch { return d; }
    },
    set(k, v) {
      try { window[kind].setItem('osu2yt.' + k, JSON.stringify(v)); } catch { /* ignore */ }
    },
  };
}
export const prefs = store('localStorage');
export const session = store('sessionStorage');

export const PER_PAGE = 50;
export const ALL_PER_PAGE = 100000;

export function busy(button, label = '処理中…') {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = label;
  return () => { button.disabled = false; button.textContent = old; };
}
