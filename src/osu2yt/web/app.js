// Shell: hash router, global status polling, job progress bar.
import { api } from './api.js';
import { h, toast, fmtTime, fill } from './util.js';
import * as library from './views/library.js';
import * as detail from './views/detail.js';
import * as settings from './views/settings.js';
import * as sync from './views/sync.js';
import * as history from './views/history.js';

const routes = [
  [/^\/library$/, library.render, 'library'],
  [/^\/song\/(.+)$/, detail.render, 'library'],
  [/^\/settings$/, settings.render, 'settings'],
  [/^\/sync$/, sync.render, 'sync'],
  [/^\/history$/, history.render, 'history'],
];

// --- global status -----------------------------------------------------------------------------

export const store = { status: null };
const statusListeners = new Set();
const jobDoneListeners = new Set();
let lastJob = null;
let pollTimer = null;

export function onStatus(fn) { statusListeners.add(fn); return () => statusListeners.delete(fn); }
export function onJobFinished(fn) { jobDoneListeners.add(fn); return () => jobDoneListeners.delete(fn); }

export async function refreshStatus() {
  clearTimeout(pollTimer);
  try {
    const st = await api('/status');
    store.status = st;
    renderTopbar(st);
    renderJobbar(st.job);
    const prev = lastJob;
    lastJob = st.job;
    if (prev && prev.status === 'running' && st.job && st.job.id === prev.id && st.job.status !== 'running') {
      const j = await api(`/jobs/${st.job.id}`);
      if (j.status === 'error') toast(`${j.label} が失敗しました: ${j.error}`, 'error', 8000);
      else if (j.status === 'cancelled') toast(`${j.label} を中断しました`, 'info');
      else if (j.kind === 'sync') toast(syncMessage(j), j.result?.status === 'paused_quota' ? 'info' : 'ok', 8000);
      else toast(j.result?.status === 'skipped' ? `${j.label}: 変更がないためスキップしました` : `${j.label} が完了しました`, 'ok');
      jobDoneListeners.forEach(fn => fn(j));
    }
    statusListeners.forEach(fn => fn(st));
  } catch (e) {
    renderJobbar(null, e.message);
  }
  const running = store.status?.job?.status === 'running';
  pollTimer = setTimeout(refreshStatus, running ? 1000 : 10000);
}

function syncMessage(j) {
  const r = j.result || {};
  if (r.dry_run) return `ドライラン完了: ${r.planned - r.unavailable} 本を追加する予定です (再生不可 ${r.unavailable})`;
  const base = `追加 ${r.added} 本 (重複 ${r.skipped_dup} / 再生不可 ${r.unavailable} / 失敗 ${r.failed})`;
  if (r.status === 'paused_quota') return `クォータ上限で一時停止: ${base}。残り ${r.remaining} 本はリセット後に再開します`;
  return `同期が完了しました: ${base}`;
}

export async function startJob(path, body, label) {
  try {
    const job = await api(path, { method: 'POST', body: body || {} });
    lastJob = { id: job.id, status: 'running' };   // so a job that finishes instantly still reports
    toast(`${label} を開始しました`);
  } catch (e) {
    toast(e.message, 'error', 6000);
  }
  refreshStatus();
}

function renderTopbar(st) {
  const q = st.quota;
  const pill = document.getElementById('quota-pill');
  pill.textContent = `クォータ ${q.used.toLocaleString()} / ${q.limit.toLocaleString()}`;
  pill.title = `本日あと約 ${q.inserts_possible} 曲追加可能 (余裕 ${q.margin} unit を確保)\nリセット: ${fmtTime(q.reset_at)}`;
  pill.className = 'pill ' + (q.available < 500 ? 'bad' : q.available < 3000 ? 'warn' : '');
  const pl = document.getElementById('playlist-pill');
  if (st.playlist) {
    pl.textContent = `▶ ${st.playlist.title || st.playlist.id}`;
    pl.href = `https://www.youtube.com/playlist?list=${encodeURIComponent(st.playlist.id)}`;
    pl.target = '_blank';
    pl.rel = 'noopener';
  } else {
    pl.textContent = 'プレイリスト未選択';
    pl.href = '#/settings';
    pl.removeAttribute('target');
  }
}

function renderJobbar(job, error) {
  const bar = document.getElementById('jobbar');
  fill(bar);
  if (error) {
    bar.hidden = false;
    bar.className = 'jobbar error';
    bar.append(h('span', {}, error));
    return;
  }
  if (!job || job.status !== 'running') { bar.hidden = true; return; }
  bar.hidden = false;
  bar.className = 'jobbar';
  const pct = job.total ? Math.round((job.done / job.total) * 100) : null;
  bar.append(
    h('strong', {}, job.label),
    h('span', { class: 'muted' }, job.message || ''),
    h('div', { class: 'progress' + (pct == null ? ' indeterminate' : '') },
      h('div', { class: 'progress-fill', style: { width: pct == null ? '30%' : `${pct}%` } })),
    h('span', { class: 'mono' }, job.total ? `${job.done} / ${job.total}` : ''),
    job.cancel_requested ? h('span', { class: 'muted' }, '中断待ち…')
      : h('button', {
        class: 'btn small ghost',
        onclick: async () => { await api(`/jobs/${job.id}/cancel`, { method: 'POST' }); refreshStatus(); },
      }, '中断'),
  );
}

// --- router --------------------------------------------------------------------------------------

let cleanup = null;

async function route() {
  const raw = location.hash.replace(/^#/, '') || '/library';
  const [path, qs] = raw.split('?');
  const params = new URLSearchParams(qs || '');
  if (typeof cleanup === 'function') cleanup();
  cleanup = null;
  const root = document.getElementById('view');
  for (const [rx, render, nav] of routes) {
    const m = path.match(rx);
    if (!m) continue;
    document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === nav));
    fill(root, h('div', { class: 'loading' }, '読み込み中…'));
    try {
      cleanup = await render(root, params, m.slice(1).map(decodeURIComponent));
    } catch (e) {
      fill(root, h('div', { class: 'empty error' }, `表示できませんでした: ${e.message}`));
    }
    return;
  }
  location.hash = '#/library';
}

// Views restore their own scroll position (the list saves it per filter/page).
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
window.addEventListener('hashchange', route);
refreshStatus().then(route);
