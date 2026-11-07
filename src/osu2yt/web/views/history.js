// Sync history: one row per run; click a run to see what happened to each video.
import { api, enc } from '../api.js';
import { onJobFinished } from '../app.js';
import { h, fill, fmtTime } from '../util.js';

export const SYNC_STATUS = {
  running: ['実行中', 'st-confirmed'],
  done: ['完了', 'st-added'],
  paused_quota: ['クォータで一時停止', 'st-needs_review'],
  cancelled: ['中断', 'st-skipped'],
  interrupted: ['中断 (終了時)', 'st-skipped'],
  error: ['エラー', 'st-gone'],
};

const QUEUE_STATE = {
  added: ['追加', 'st-added'],
  planned: ['予定', 'st-confirmed'],
  pending: ['未処理', 'st-unmatched'],
  carried_over: ['次回へ持ち越し', 'st-unmatched'],
  dup: ['追加済みのためスキップ', 'st-skipped'],
  unavailable: ['再生不可', 'st-gone'],
  failed: ['失敗', 'st-gone'],
};

const TRIGGER = { manual: 'WebUI', auto_resume: '自動再開', cli: 'CLI' };

const badge = ([label, cls]) => h('span', { class: `badge ${cls}` }, label);
export const syncBadge = status => badge(SYNC_STATUS[status] || [status, 'st-unmatched']);

export async function render(root, params) {
  const selected = params.get('run') ? Number(params.get('run')) : null;
  let runs = await api('/sync/runs');
  const detailBox = h('div');

  async function showRun(id) {
    fill(detailBox, h('div', { class: 'loading' }, '読み込み中…'));
    const d = await api(`/sync/runs/${id}`);
    const counts = {};
    d.items.forEach(it => { counts[it.state] = (counts[it.state] || 0) + 1; });
    fill(detailBox, h('div', { class: 'card' },
      h('h3', {}, `#${d.id} の詳細 `, d.dry_run ? h('span', { class: 'tag' }, 'ドライラン') : null),
      h('p', { class: 'muted small' }, `${fmtTime(d.started_at)} → ${d.finished_at ? fmtTime(d.finished_at) : '-'} · `,
        d.playlist_title || d.playlist_id, ` · ${TRIGGER[d.trigger] || d.trigger || ''}`),
      d.error ? h('p', { class: 'note' }, d.error) : null,
      h('div', { class: 'chips' }, Object.entries(counts).map(([st, n]) =>
        h('span', { class: 'chip' }, `${(QUEUE_STATE[st] || [st])[0]} ${n}`))),
      d.items.length ? h('table', { class: 'table' }, h('tbody', {}, d.items.map(it => h('tr', {},
        h('td', {}, badge(QUEUE_STATE[it.state] || [it.state, 'st-unmatched'])),
        h('td', {}, h('a', { href: `#/song/${enc(it.set_key)}` }, it.title_unicode || it.set_key),
          h('div', { class: 'muted small' }, it.artist_unicode || '')),
        h('td', { class: 'small' }, h('a', {
          href: `https://www.youtube.com/watch?v=${enc(it.video_id)}`, target: '_blank', rel: 'noopener',
        }, it.video_title || it.video_id),
        it.last_error ? h('div', { class: 'muted small' }, it.last_error) : null)))))
        : h('p', { class: 'muted' }, '追加対象はありませんでした。'),
    ));
  }

  function draw() {
    fill(root,
      h('div', { class: 'card' },
        h('h2', {}, '同期の履歴'),
        runs.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'table clickable' },
          h('thead', {}, h('tr', {},
            ['#', '開始', '起動', '状態', '予定', '追加', '重複', '再生不可', '失敗', '残り', 'unit'].map(t => h('th', {}, t)))),
          h('tbody', {}, runs.map(r => h('tr', {
            class: r.id === selected ? 'active' : '',
            onclick: () => { location.hash = `#/history?run=${r.id}`; },
          },
          h('td', { class: 'mono' }, String(r.id)),
          h('td', { class: 'small' }, fmtTime(r.started_at)),
          h('td', { class: 'small' }, TRIGGER[r.trigger] || r.trigger || '', r.dry_run ? h('span', { class: 'tag' }, 'ドライラン') : null),
          h('td', {}, syncBadge(r.status)),
          ...[r.planned, r.added, r.skipped_dup, r.unavailable, r.failed, r.remaining, r.quota_used]
            .map(v => h('td', { class: 'mono' }, v == null ? '-' : String(v))))))))
          : h('p', { class: 'muted' }, 'まだ同期していません。'),
      ),
      detailBox,
    );
  }

  draw();
  if (selected) await showRun(selected).catch(() => fill(detailBox));
  const off = onJobFinished(async j => {
    if (j.kind !== 'sync') return;
    runs = await api('/sync/runs');
    draw();
    if (selected) showRun(selected);
  });
  return off;
}
