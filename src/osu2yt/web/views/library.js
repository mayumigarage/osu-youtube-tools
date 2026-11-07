// Library list: tabs by state, search, filters, pagination (or show-all), optional split view that
// shows the candidate picker for the selected song next to the list.
import { api, enc } from '../api.js';
import { onJobFinished, startJob } from '../app.js';
import {
  h, fmtDur, fmtDiff, stateBadge, reasonLabel, debounce, REASON_LABEL, fill, prefs, session,
  PER_PAGE, ALL_PER_PAGE } from '../util.js';
import * as detail from './detail.js';
import { invalidateKeys, filterQuery } from './detail.js';

const TABS = [
  ['all', 'すべて'],
  ['needs_review', '要確認'],
  ['confirmed', '確定'],
  ['added', '追加済み'],
  ['unmatched', '未マッチ'],
  ['skipped', 'スキップ'],
  ['gone', '消えた'],
];

const SORTS = [
  ['added_desc', '追加日 (新しい順)'],
  ['added_asc', '追加日 (古い順)'],
  ['score_asc', 'スコア (低い順)'],
  ['score_desc', 'スコア (高い順)'],
  ['artist', 'アーティスト'],
  ['title', '曲名'],
  ['duration', '長さ'],
];

export async function render(root, params) {
  invalidateKeys();
  const st = {
    view: params.get('view') || 'all',
    q: params.get('q') || '',
    pack: params.get('pack') === '1',
    reason: params.get('reason') || '',
    sort: params.get('sort') || 'added_desc',
    page: Number(params.get('page')) || 1,
    song: params.get('song') || '',
  };
  let showAll = prefs.get('showAll', false);
  let split = prefs.get('split', false);
  const lastSong = session.get('lastSong');

  const tabs = h('div', { class: 'tabs' });
  const search = h('input', {
    type: 'search', class: 'input', placeholder: '曲名・アーティスト・ソース・動画タイトル', value: st.q,
    oninput: debounce(() => { st.q = search.value.trim(); st.page = 1; load(); }, 250),
  });
  const sort = h('select', { class: 'input', onchange: () => { st.sort = sort.value; st.page = 1; load(); } },
    SORTS.map(([v, l]) => h('option', { value: v, selected: v === st.sort }, l)));
  const pack = h('label', { class: 'check' },
    h('input', { type: 'checkbox', checked: st.pack, onchange: e => { st.pack = e.target.checked; st.page = 1; load(); } }),
    'パック譜面のみ');
  const allToggle = h('label', { class: 'check', title: 'ページを分けずに全曲を 1 ページに表示' },
    h('input', {
      type: 'checkbox', checked: showAll,
      onchange: e => { showAll = e.target.checked; prefs.set('showAll', showAll); st.page = 1; load(); },
    }), '全件表示');
  const splitToggle = h('label', { class: 'check', title: '一覧の横に候補選択を表示' },
    h('input', {
      type: 'checkbox', checked: split,
      onchange: e => { split = e.target.checked; prefs.set('split', split); applySplit(); },
    }), '分割表示');
  const reasons = h('div', { class: 'chips' });
  const list = h('div', { class: 'songlist', onclick: onListClick });
  const pager = h('div', { class: 'pager' });
  const summary = h('div', { class: 'muted small' });
  const banner = h('div');
  const songPane = h('div', { class: 'song-pane' });
  const body = h('div', { class: 'library-body' },
    h('div', { class: 'list-pane' }, reasons, list, pager),
    songPane);

  fill(root, h('div', { class: 'library' },
    banner,
    h('div', { class: 'toolbar' }, tabs),
    h('div', { class: 'toolbar' }, search, sort, pack, allToggle, splitToggle, summary),
    body,
  ));

  // --- URL + scroll position ----------------------------------------------------------------------
  function listQuery() {
    const qs = filterQuery(st);
    if (!showAll && st.page > 1) qs.set('page', st.page);
    return qs;
  }

  function syncHash() {
    const qs = listQuery();
    if (split && st.song) qs.set('song', st.song);
    history.replaceState(null, '', `#/library${qs.toString() ? '?' + qs : ''}`);
  }

  const scrollKey = () => `scroll:${listQuery()}|${showAll ? 'all' : 'paged'}`;
  // Saved on teardown (route change runs cleanup before the list DOM is replaced) and on reload/close.
  const saveScroll = () => session.set(scrollKey(), window.scrollY);
  window.addEventListener('pagehide', saveScroll);

  function restoreScroll() {
    const y = session.get(scrollKey());
    if (y != null) window.scrollTo(0, y);
    // The song last opened in the detail view (it may have moved there with ←/→): keep it in view.
    const row = lastSong && rowFor(lastSong);
    if (!row) return;
    row.classList.add('last-viewed');
    const r = row.getBoundingClientRect();
    if (r.top < 110 || r.bottom > window.innerHeight) row.scrollIntoView({ block: 'center' });
  }

  // --- list -------------------------------------------------------------------------------------------
  let first = true;
  async function load() {
    syncHash();
    invalidateKeys();
    let data;
    try {
      data = await api('/library', {
        query: {
          view: st.view, q: st.q, pack: st.pack, reason: st.reason, sort: st.sort,
          page: showAll ? 1 : st.page, per_page: showAll ? ALL_PER_PAGE : PER_PAGE,
        },
      });
    } catch (e) {
      fill(list, h('div', { class: 'empty error' }, e.message));
      return;
    }
    renderTabs(data.counts);
    renderBanner(data.counts);
    renderReasons(data.reasons);
    summary.textContent = `${data.total} 曲`;
    if (!data.items.length) {
      fill(list, emptyState(data.counts));
    } else {
      fill(list, ...data.items.map(row));
    }
    renderPager(data.total, data.per_page);
    markActive();
    if (first) { first = false; restoreScroll(); }
  }

  function renderTabs(counts) {
    fill(tabs, ...TABS.map(([v, label]) => h('button', {
      class: 'tab' + (v === st.view ? ' active' : '') + (v === 'needs_review' && counts[v] ? ' attention' : ''),
      onclick: () => {
        st.view = v; st.page = 1; st.reason = '';
        if (v === 'needs_review' && st.sort === 'added_desc') { st.sort = 'score_asc'; sort.value = st.sort; }
        load();
      },
    }, label, h('span', { class: 'count' }, String(counts[v] || 0)))));
  }

  function renderReasons(rs) {
    if (st.view !== 'needs_review' || !rs || !Object.keys(rs).length) { fill(reasons); return; }
    const chip = (value, label, n) => h('button', {
      class: 'chip' + (st.reason === value ? ' active' : ''),
      onclick: () => { st.reason = st.reason === value ? '' : value; st.page = 1; load(); },
    }, label, n != null ? h('span', { class: 'count' }, String(n)) : null);
    fill(reasons,
      h('span', { class: 'muted small' }, '理由:'),
      ...Object.entries(rs).sort((a, b) => b[1] - a[1]).map(([r, n]) => chip(r, REASON_LABEL[r] || r, n)),
    );
  }

  function renderPager(total, per) {
    fill(pager);
    if (showAll) return;
    const pages = Math.max(1, Math.ceil(total / per));
    if (pages <= 1) return;
    const go = p => { st.page = p; load(); window.scrollTo(0, 0); };
    pager.append(
      h('button', { class: 'btn small', disabled: st.page <= 1, onclick: () => go(st.page - 1) }, '← 前'),
      h('span', { class: 'muted' }, `${st.page} / ${pages}`),
      h('button', { class: 'btn small', disabled: st.page >= pages, onclick: () => go(st.page + 1) }, '次 →'),
    );
  }

  function row(it) {
    const v = it.video;
    const diff = v ? fmtDiff(v.duration_sec, it.duration_sec) : null;
    const romaji = (it.title !== it.title_unicode || it.artist !== it.artist_unicode) ? `${it.artist} - ${it.title}` : null;
    const qs = filterQuery(st);
    const href = `#/song/${enc(it.key)}${qs.toString() ? '?' + qs : ''}`;
    return h('a', { class: 'songrow', href, dataset: { key: it.key } },
      h('div', { class: 'thumb' }, v ? h('img', { src: v.thumbnail_url, alt: '', loading: 'lazy' }) : h('div', { class: 'thumb-empty' }, '♪')),
      h('div', { class: 'song' },
        h('div', { class: 'title' }, it.title_unicode, it.is_pack ? h('span', { class: 'tag' }, 'パック') : null),
        h('div', { class: 'artist' }, it.artist_unicode),
        romaji ? h('div', { class: 'muted small ellipsis' }, romaji) : null,
        it.source ? h('div', { class: 'muted small ellipsis' }, it.source) : null),
      h('div', { class: 'len mono' }, fmtDur(it.duration_sec)),
      h('div', { class: 'video' },
        v ? [
          h('div', { class: 'ellipsis' }, v.title || v.id),
          h('div', { class: 'muted small ellipsis' },
            v.channel || '', v.channel_verified ? ' ✓' : '', '  ',
            h('span', { class: 'mono' }, fmtDur(v.duration_sec)), ' ',
            diff ? h('span', { class: `diff ${diff.cls}` }, diff.text) : null),
        ] : h('span', { class: 'muted' }, '—')),
      h('div', { class: 'score mono' }, it.match_score != null ? it.match_score.toFixed(2) : ''),
      h('div', { class: 'state' },
        stateBadge(it.state, it.review_reason),
        it.state === 'needs_review' && it.review_reason ? h('div', { class: 'muted small' }, reasonLabel(it.review_reason)) : null),
    );
  }

  const rowFor = key => list.querySelector(`.songrow[data-key="${CSS.escape(key)}"]`);

  function emptyState(counts) {
    if (!counts.all) {
      return h('div', { class: 'empty' },
        h('p', {}, 'まだライブラリを読み込んでいません。'),
        h('button', { class: 'btn primary', onclick: () => startJob('/library/scan', {}, 'スキャン') }, 'osu!lazer をスキャン'));
    }
    return h('div', { class: 'empty' }, '該当する曲はありません');
  }

  function renderBanner(counts) {
    if (!counts.unmatched) { fill(banner); return; }
    fill(banner, h('div', { class: 'banner' },
      `YouTube 未検索の曲が ${counts.unmatched} 曲あります。`,
      h('button', { class: 'btn small primary', onclick: () => startJob('/match', { scope: 'unmatched' }, 'マッチング') },
        '検索を開始')));
  }

  // --- split view -------------------------------------------------------------------------------------
  let songCleanup = null;
  let songSeq = 0;

  function onListClick(e) {
    if (!split || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest('.songrow');
    if (!a) return;
    e.preventDefault();
    openSong(a.dataset.key);
  }

  function markActive() {
    list.querySelectorAll('.songrow.active').forEach(el => el.classList.remove('active'));
    if (split && st.song) rowFor(st.song)?.classList.add('active');
  }

  async function openSong(key, { reveal = false } = {}) {
    st.song = key;
    session.set('lastSong', key);
    syncHash();
    markActive();
    if (reveal) rowFor(key)?.scrollIntoView({ block: 'nearest' });
    const seq = ++songSeq;
    if (songCleanup) { songCleanup(); songCleanup = null; }
    if (!songPane.firstChild || songPane.querySelector('.empty')) fill(songPane, h('div', { class: 'loading' }, '読み込み中…'));
    const pane = h('div');
    let cleanup;
    try {
      cleanup = await detail.render(pane, filterQuery(st), [key], {
        embedded: true,
        navigate: k => openSong(k, { reveal: true }),
        close: closeSong,
        onChange: updateRow,
      });
    } catch (e) {
      if (seq === songSeq) fill(songPane, h('div', { class: 'empty error' }, `表示できませんでした: ${e.message}`));
      return;
    }
    if (seq !== songSeq) { cleanup?.(); return; }   // a newer song was opened meanwhile
    songCleanup = cleanup;
    fill(songPane, pane);
    songPane.scrollTop = 0;
  }

  function closeSong() {
    songSeq++;
    if (songCleanup) { songCleanup(); songCleanup = null; }
    st.song = '';
    syncHash();
    markActive();
    fill(songPane, h('div', { class: 'empty' }, '一覧の曲をクリックすると、ここで候補を選べます'));
  }

  function updateRow(song) {
    const old = rowFor(song.key);
    if (!old) return;
    const fresh = row(song);
    fresh.className = old.className;
    old.replaceWith(fresh);
  }

  function applySplit() {
    body.parentElement.classList.toggle('split', split);
    songPane.hidden = !split;
    if (split && st.song) openSong(st.song);
    else if (split) closeSong();
    else {
      songSeq++;
      if (songCleanup) { songCleanup(); songCleanup = null; }
      fill(songPane);
      syncHash();
      markActive();
    }
  }

  const off = onJobFinished(() => load());
  applySplit();
  await load();
  return () => {
    off();
    saveScroll();
    window.removeEventListener('pagehide', saveScroll);
    if (songCleanup) songCleanup();
    songSeq++;
  };
}
