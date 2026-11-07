// Song detail: candidate list with embedded preview, adopt / confirm / skip / re-search / manual URL.
// Keyboard: ↑↓ (j/k) select, Enter adopt, C confirm, S skip, R re-search, ←→ prev/next, Esc list.
import { api, enc } from '../api.js';
import { refreshStatus } from '../app.js';
import {
  h, fmtDur, fmtDiff, stateBadge, reasonLabel, toast, osuUrl, ytUrl, busy, NOTE_LABEL, fill, prefs, session,
  PER_PAGE } from '../util.js';

let keysCache = { filter: null, keys: [] };
export function invalidateKeys() { keysCache = { filter: null, keys: [] }; }

// Filter state <-> query string shared by the list and the detail view.
export function filterQuery(st) {
  const qs = new URLSearchParams();
  if (st.view && st.view !== 'all') qs.set('view', st.view);
  if (st.q) qs.set('q', st.q);
  if (st.pack) qs.set('pack', '1');
  if (st.reason) qs.set('reason', st.reason);
  if (st.sort && st.sort !== 'added_desc') qs.set('sort', st.sort);
  return qs;
}

const PART_LABEL = { duration: '長さ', title: '曲名', artist: '歌手', official: '公式' };
const BY_LABEL = { auto: '自動', user: '候補から選択', manual: 'URL 指定' };
const AVAIL_LABEL = { deleted: '削除済み', private: '非公開', region_blocked: '地域制限', not_embeddable: '埋め込み不可' };

// opts.embedded: rendered inside the library's split view instead of as its own page; then
// navigate(key) / close() replace hash navigation and onChange(song) is told about every update.
export async function render(root, params, [key], opts = {}) {
  const embedded = !!opts.embedded;
  const filter = {
    view: params.get('view') || 'all', q: params.get('q') || '', pack: params.get('pack') === '1',
    reason: params.get('reason') || '', sort: params.get('sort') || 'added_desc',
  };
  const filterStr = filterQuery(filter).toString();
  if (keysCache.filter !== filterStr) {
    keysCache = { filter: filterStr, keys: await api('/library/keys', { query: filter }) };
  }
  const keys = keysCache.keys;
  const idx = keys.indexOf(key);

  let song = await api(`/library/${enc(key)}`);
  let selected = pickDefault(song);
  let playing = null;

  if (!embedded) session.set('lastSong', key);
  // Back to the list page that contains this song (it may differ from where we came from after ←/→).
  const listQs = new URLSearchParams(filterStr);
  const page = idx >= 0 && !prefs.get('showAll', false) ? Math.floor(idx / PER_PAGE) + 1 : 1;
  if (page > 1) listQs.set('page', page);
  const listHref = `#/library${listQs.toString() ? '?' + listQs : ''}`;
  const goto = embedded ? opts.navigate : k => { location.hash = `#/song/${enc(k)}${filterStr ? '?' + filterStr : ''}`; };
  const close = embedded ? opts.close : () => { location.hash = listHref; };
  const hasNext = idx >= 0 && idx + 1 < keys.length;
  const next = () => {
    if (hasNext) goto(keys[idx + 1]);
    else { toast('一覧の最後の曲でした'); if (!embedded) close(); }
  };
  const prev = () => { if (idx > 0) goto(keys[idx - 1]); };

  // --- layout (player is persistent so re-renders do not restart the video) --------------------
  const header = h('div', { class: 'detail-header' });
  const player = h('div', { class: 'player' }, h('div', { class: 'player-empty' }, '候補をクリックするとここで再生します'));
  const selectedBox = h('div', { class: 'selected-box' });
  const info = h('div', { class: 'card' });
  const actions = h('div', { class: 'card' });
  const cands = h('div', { class: 'cands' });

  fill(root, 
    header,
    h('div', { class: 'detail' + (embedded ? ' embedded' : '') },
      h('div', { class: 'detail-left' }, player, selectedBox, info, actions),
      h('div', { class: 'detail-right' }, h('h3', {}, '候補'), cands)),
  );

  function pickDefault(s) {
    return (s.candidates.find(c => c.chosen) || s.candidates.find(c => !c.availability || c.availability === 'ok') || s.candidates[0])?.video_id || null;
  }

  function setPlayer(id, autoplay) {
    if (!id || playing === id) return;
    playing = id;
    fill(player, h('iframe', {
      src: `https://www.youtube-nocookie.com/embed/${enc(id)}?rel=0&autoplay=${autoplay ? 1 : 0}`,
      title: 'YouTube プレビュー', allow: 'autoplay; encrypted-media; picture-in-picture; fullscreen',
      referrerpolicy: 'strict-origin-when-cross-origin',
    }));
  }

  async function act(label, fn, { advance = false } = {}) {
    try {
      song = await fn();
      opts.onChange?.(song);
      toast(label, 'ok', 2000);
      refreshStatus();
      if (advance && hasNext) { next(); return; }
      selected = pickDefault(song);
      draw();
    } catch (e) {
      toast(e.message, 'error', 6000);
    }
  }

  const adopt = videoId => act('採用しました', () => api(`/library/${enc(key)}/choose`, { method: 'POST', body: { video: videoId } }), { advance: true });
  const confirmCurrent = () => act('確定しました', () => api(`/library/${enc(key)}/confirm`, { method: 'POST' }), { advance: true });
  const skip = () => act('スキップしました', () => api(`/library/${enc(key)}/skip`, { method: 'POST' }), { advance: true });
  const unskip = () => act('スキップを解除しました', () => api(`/library/${enc(key)}/unskip`, { method: 'POST' }));
  let rematchBtn;
  const rematch = async () => {
    const done = rematchBtn ? busy(rematchBtn, '検索中…') : () => {};
    await act('再検索しました', () => api(`/library/${enc(key)}/rematch`, { method: 'POST' }));
    done();
  };

  // --- drawing --------------------------------------------------------------------------------
  function draw() {
    fill(header, 
      embedded
        ? h('button', { class: 'btn small ghost', onclick: close, title: 'Esc' }, '✕ 閉じる')
        : h('a', { class: 'btn small ghost', href: listHref }, '← 一覧'),
      h('span', { class: 'muted' }, idx >= 0 ? `${idx + 1} / ${keys.length}` : ''),
      h('button', { class: 'btn small', disabled: idx <= 0, onclick: prev }, '◀ 前'),
      h('button', { class: 'btn small', disabled: !hasNext, onclick: next }, '次 ▶'),
      h('span', { class: 'kbd-help muted small' },
        '↑↓ 候補選択 · Enter 採用 · C 確定 · S スキップ · R 再検索 · ←→ 前後'),
      embedded ? h('a', { class: 'btn small ghost', href: `#/song/${enc(key)}${filterStr ? '?' + filterStr : ''}`, title: '単独ページで開く' }, '⤢') : null,
    );

    drawInfo();
    drawActions();
    drawSelected();
    drawCandidates();
    setPlayer(selected, false);
  }

  function drawInfo() {
    const romaji = (song.title !== song.title_unicode || song.artist !== song.artist_unicode) ? `${song.artist} - ${song.title}` : null;
    const osu = osuUrl(song.online_id);
    fill(info, 
      h('div', { class: 'song-title' }, song.title_unicode, song.is_pack ? h('span', { class: 'tag' }, 'パック') : null),
      h('div', { class: 'song-artist' }, song.artist_unicode),
      romaji ? h('div', { class: 'muted' }, romaji) : null,
      h('dl', { class: 'kv' },
        song.source ? [h('dt', {}, 'ソース'), h('dd', {}, song.source)] : null,
        h('dt', {}, '長さ'), h('dd', {}, fmtDur(song.duration_sec), h('span', { class: 'muted small' },
          song.duration_source === 'audio' ? ' (音声ファイル)' : song.duration_source === 'map' ? ' (譜面の長さ)' : '')),
        h('dt', {}, '状態'), h('dd', {}, stateBadge(song.state, song.review_reason), ' ',
          song.review_reason && song.state === 'needs_review' ? h('span', { class: 'muted small' }, reasonLabel(song.review_reason)) : null,
          song.chosen_by && song.video ? h('span', { class: 'muted small' }, ` 採用: ${BY_LABEL[song.chosen_by] || song.chosen_by}`) : null),
        h('dt', {}, 'ID'), h('dd', {}, osu ? h('a', { href: osu, target: '_blank', rel: 'noopener' }, song.key) : song.key),
      ),
      song.same_video_sets.length ? h('div', { class: 'note' },
        '同じ動画を採用している譜面: ',
        song.same_video_sets.map(s => h('a', { href: `#/song/${enc(s.key)}` }, `${s.title_unicode} (${s.key})`)).flatMap((a, i) => i ? [', ', a] : [a]),
        h('div', { class: 'muted small' }, 'プレイリストには 1 回だけ追加されます')) : null,
      song.variants.length > 1 ? h('details', { class: 'variants', open: song.is_pack },
        h('summary', {}, `含まれる音源 ${song.variants.length} 件`),
        h('table', { class: 'table small' },
          h('tbody', {}, song.variants.map(v => h('tr', {},
            h('td', {}, v.titleUnicode || v.title), h('td', { class: 'muted' }, v.audioFile),
            h('td', { class: 'mono' }, fmtDur((v.maxLengthMs || 0) / 1000))))))) : null,
    );
  }

  function drawActions() {
    const url = h('input', { class: 'input', placeholder: 'YouTube の URL または動画 ID', spellcheck: 'false' });
    rematchBtn = h('button', { class: 'btn', onclick: rematch, title: 'R' }, '再検索');
    fill(actions, 
      h('div', { class: 'btnrow' },
        song.state === 'needs_review' && song.video
          ? h('button', { class: 'btn primary', onclick: confirmCurrent, title: 'C' }, '採用中の動画で確定') : null,
        song.match_status === 'skipped'
          ? h('button', { class: 'btn', onclick: unskip }, 'スキップ解除')
          : h('button', { class: 'btn', onclick: skip, title: 'S' }, 'この曲はスキップ'),
        rematchBtn),
      h('form', {
        class: 'urlform',
        onsubmit: e => { e.preventDefault(); if (url.value.trim()) adopt(url.value.trim()); },
      }, url, h('button', { class: 'btn', type: 'submit' }, 'URL を指定して採用')),
    );
  }

  function drawSelected() {
    const c = song.candidates.find(x => x.video_id === selected);
    if (!c) { fill(selectedBox); return; }
    fill(selectedBox, 
      h('div', { class: 'ellipsis' }, h('a', { href: ytUrl(c.video_id), target: '_blank', rel: 'noopener' }, c.title)),
      c.chosen ? h('span', { class: 'badge st-confirmed' }, '採用中')
        : h('button', { class: 'btn primary small', onclick: () => adopt(c.video_id), title: 'Enter' }, 'この動画を採用'),
    );
  }

  function drawCandidates() {
    if (!song.candidates.length) {
      fill(cands, h('div', { class: 'empty' }, '候補がありません。再検索するか URL を指定してください。'));
      return;
    }
    fill(cands, ...song.candidates.map(c => {
      const diff = fmtDiff(c.duration_sec, song.duration_sec);
      const penalties = Object.entries(c.parts).filter(([k]) => k.startsWith('penalty'));
      const notes = c.notes.filter(n => !n.startsWith('words:') && !n.startsWith('chart:')).map(n => NOTE_LABEL[n] || n);
      const words = c.notes.find(n => n.startsWith('words:'));
      const chart = c.notes.find(n => n.startsWith('chart:'));
      const el = h('div', {
        class: 'cand' + (c.video_id === selected ? ' selected' : '') + (c.chosen ? ' chosen' : '')
          + (c.availability && !['ok', 'not_embeddable'].includes(c.availability) ? ' dead' : ''),
        onclick: () => { selected = c.video_id; drawSelected(); markSelected(); setPlayer(c.video_id, true); },
        ondblclick: () => adopt(c.video_id),
        dataset: { vid: c.video_id },
      },
        h('img', { class: 'cand-thumb', src: c.thumbnail_url, alt: '', loading: 'lazy' }),
        h('div', { class: 'cand-body' },
          h('div', { class: 'cand-title' }, c.title),
          h('div', { class: 'muted small' },
            c.channel, c.channel_verified ? ' ✓' : '', ' · ',
            h('span', { class: 'mono' }, fmtDur(c.duration_sec)), ' ',
            diff ? h('span', { class: `diff ${diff.cls}` }, diff.text) : null),
          h('div', { class: 'bars' }, Object.entries(PART_LABEL).map(([k, label]) => {
            const v = c.parts[k] ?? 0;
            return h('div', { class: 'bar', title: `${label} ${v.toFixed(2)}` },
              h('span', {}, label), h('div', { class: 'bar-track' }, h('div', { class: 'bar-fill', style: { width: `${Math.round(v * 100)}%` } })));
          })),
          h('div', { class: 'chips' },
            c.chosen ? h('span', { class: 'chip ok' }, '採用中') : null,
            c.manual ? h('span', { class: 'chip' }, '手動指定') : null,
            notes.map(n => h('span', { class: 'chip' }, n)),
            words ? h('span', { class: 'chip bad' }, `減点: ${words.slice(6)}`) : null,
            chart ? h('span', { class: 'chip bad', title: '音ゲーのプレイ・譜面確認動画らしい' }, `譜面/プレイ動画: ${chart.slice(6)}`) : null,
            penalties.length && !words && !chart ? h('span', { class: 'chip bad' }, '減点あり') : null,
            c.availability && c.availability !== 'ok' ? h('span', { class: 'chip bad' }, AVAIL_LABEL[c.availability] || c.availability) : null)),
        h('div', { class: 'cand-side' },
          h('div', { class: 'cand-score mono' }, c.score != null ? c.score.toFixed(2) : '—'),
          c.chosen ? null : h('button', {
            class: 'btn small', onclick: e => { e.stopPropagation(); adopt(c.video_id); },
          }, '採用')),
      );
      return el;
    }));
  }

  function markSelected() {
    cands.querySelectorAll('.cand').forEach(el => el.classList.toggle('selected', el.dataset.vid === selected));
    cands.querySelector('.cand.selected')?.scrollIntoView({ block: 'nearest' });
  }

  function onKey(e) {
    if (e.target.closest('input, textarea, select') || e.ctrlKey || e.metaKey || e.altKey) return;
    const ids = song.candidates.map(c => c.video_id);
    const i = ids.indexOf(selected);
    switch (e.key) {
      case 'ArrowDown': case 'j':
        if (ids.length) { selected = ids[Math.min(ids.length - 1, i + 1)]; drawSelected(); markSelected(); setPlayer(selected, true); }
        break;
      case 'ArrowUp': case 'k':
        if (ids.length) { selected = ids[Math.max(0, i - 1)]; drawSelected(); markSelected(); setPlayer(selected, true); }
        break;
      case 'Enter': if (selected) adopt(selected); break;
      case 'c': case 'C': if (song.state === 'needs_review' && song.video) confirmCurrent(); break;
      case 's': case 'S': skip(); break;
      case 'r': case 'R': rematch(); break;
      case 'ArrowRight': next(); break;
      case 'ArrowLeft': prev(); break;
      case 'Escape': close(); break;
      default: return;
    }
    e.preventDefault();
  }

  document.addEventListener('keydown', onKey);
  draw();
  return () => document.removeEventListener('keydown', onKey);
}
