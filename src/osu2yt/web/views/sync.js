// Sync: local preview with quota estimate → confirm → background job. Paused runs resume after the PT reset.
import { api, enc } from '../api.js';
import { store, startJob, onJobFinished, onStatus } from '../app.js';
import { h, fill, fmtTime, toast } from '../util.js';

const SHOW_ITEMS = 100;

export async function render(root) {
  let maxAdd = '';
  let showAll = false;
  let data = await load();

  async function load() {
    return api('/sync/preview', { query: { max_add: maxAdd || undefined } });
  }
  async function reload() {
    data = await load();
    draw();
  }

  const running = () => store.status?.job?.status === 'running';

  function start(dryRun) {
    const e = data.estimate;
    if (!dryRun) {
      const lines = [
        `「${data.playlist.title}」に ${e.count} 本を追加します。`,
        `推定クォータ: 最大 ${e.total_units.toLocaleString()} unit (今日の残り ${e.available_today.toLocaleString()} unit)`,
        e.count > e.inserts_today
          ? `今日は約 ${e.inserts_today} 本まで。残りはクォータ上限で一時停止し、リセット (${fmtTime(e.reset_at)}) 後に再開します。`
          : '今日中に終わる見込みです。',
        'プレイリストから削除することはありません。',
      ];
      if (!confirm(lines.join('\n'))) return;
    }
    startJob('/sync', { dry_run: dryRun, max_add: maxAdd ? Number(maxAdd) : null }, dryRun ? 'ドライラン' : '同期');
  }

  function blocker() {
    if (!data.playlist) return h('div', { class: 'banner' }, '同期先のプレイリストが選ばれていません。', h('a', { href: '#/settings' }, '設定で選ぶ'));
    if (!data.logged_in) return h('div', { class: 'banner' }, 'YouTube にログインしていません。', h('a', { href: '#/settings' }, '設定でログイン'));
    return null;
  }

  function resumeBanner() {
    const last = data.last_run;
    if (!last || last.status !== 'paused_quota') return null;
    const reset = fmtTime(data.estimate.reset_at);
    let next;
    if (data.resume_due) {
      next = data.auto_resume ? ' クォータがリセットされたので、まもなく自動で再開します。'
        : ' クォータがリセットされました。「同期を開始」で続きから追加します。';
    } else {
      next = data.auto_resume ? ` ${reset} のリセット後、osu2yt が起動していれば自動で再開します。`
        : ` ${reset} のリセット後に「同期を開始」で続きから追加できます。`;
    }
    return h('div', { class: 'note' },
      h('strong', {}, `前回 (#${last.id}) はクォータ上限で一時停止しました`),
      ` — 追加 ${last.added} 本、残り ${data.summary.to_add} 本。`, next);
  }

  function draw() {
    const s = data.summary;
    const e = data.estimate;
    const blocked = blocker();
    const busyNow = running();
    const pl = data.playlist;
    const max = h('input', {
      class: 'input narrow', type: 'number', min: '1', placeholder: 'すべて', value: maxAdd,
      onchange: async ev => { maxAdd = ev.target.value; await reload(); },
    });
    const items = showAll ? data.items : data.items.slice(0, SHOW_ITEMS);

    fill(root,
      h('div', { class: 'card' },
        h('div', { class: 'row spread' },
          h('h2', {}, '同期'),
          pl ? h('a', { href: `https://www.youtube.com/playlist?list=${enc(pl.id)}`, target: '_blank', rel: 'noopener' },
            `▶ ${pl.title || pl.id}`) : null),
        blocked,
        resumeBanner(),
        h('dl', { class: 'kv big' },
          h('dt', {}, '確定した譜面'), h('dd', {}, `${s.confirmed_sets} 譜面 → 動画 ${s.unique_videos} 本`,
            h('span', { class: 'muted small' }, ' (同じ動画は 1 回だけ追加)')),
          h('dt', {}, 'プレイリストに追加済み'), h('dd', {}, `${s.already_present} 本`),
          s.removed_remote ? [h('dt', {}, 'YouTube 上で削除済み'), h('dd', {}, `${s.removed_remote} 本`,
            h('span', { class: 'muted small' }, ' (手動で外したものとみなし、再追加しません)'))] : null,
          s.unavailable ? [h('dt', {}, '再生できない動画'), h('dd', {}, `${s.unavailable} 本 (除外)`)] : null,
          h('dt', {}, '要確認 (対象外)'), h('dd', {}, h('a', { href: '#/library?view=needs_review&sort=score_asc' }, `${s.needs_review} 曲`)),
          h('dt', {}, '追加予定'), h('dd', {}, h('strong', {}, `${e.count} 本`),
            s.to_add !== e.count ? h('span', { class: 'muted small' }, ` (全 ${s.to_add} 本のうち)`) : null),
        ),
        h('p', { class: 'muted small' }, s.pulled_at
          ? `プレイリストの中身は ${fmtTime(s.pulled_at)} 時点の記録です。同期の最初に YouTube から最新の中身を取得し、追加済みのものは除きます。`
          : '同期の最初に YouTube からプレイリストの中身を取得し、追加済みのものは除きます。'),
      ),

      e.count ? h('div', { class: 'card' },
        h('h3', {}, 'クォータの見積もり'),
        h('dl', { class: 'kv' },
          h('dt', {}, '合計'), h('dd', {}, h('strong', {}, `${e.total_units.toLocaleString()} unit`),
            h('span', { class: 'muted small' },
              ` = 中身の取得 ${e.pull_units} + 生存確認 ${e.check_units} + 追加 ${e.count} × 50`)),
          h('dt', {}, '今日の残り'), h('dd', {}, `${e.available_today.toLocaleString()} unit → 約 ${e.inserts_today} 本追加できます`),
          h('dt', {}, '所要日数'), h('dd', {}, `約 ${e.days} 日`,
            h('span', { class: 'muted small' }, ` (1 日あたり約 ${e.per_day} 本、リセットは ${fmtTime(e.reset_at)})`)),
          h('dt', {}, '今回の上限'), h('dd', {}, max, h('span', { class: 'muted small' }, ' 本 (空欄ならすべて)')),
        ),
        h('div', { class: 'btnrow' },
          h('button', { class: 'btn primary', disabled: !!blocked || busyNow, onclick: () => start(false) },
            `同期を開始 (${e.count} 本)`),
          h('button', {
            class: 'btn', disabled: !!blocked || busyNow, onclick: () => start(true),
            title: 'プレイリストの取得と生存確認だけ行い、追加はしません',
          }, `ドライラン (約 ${e.pull_units + e.check_units} unit)`),
          busyNow ? h('span', { class: 'muted small' }, '処理の実行中は開始できません') : null),
      ) : h('div', { class: 'card' },
        blocked ? null : h('p', {}, '追加するものはありません。新しい譜面を入れたら「設定 → スキャン」→ マッチングの後にここへ戻ってください。'),
        h('div', { class: 'btnrow' },
          h('button', { class: 'btn', disabled: !!blocked || busyNow, onclick: () => start(true) },
            'ドライラン (YouTube 側の中身と照合)'))),

      data.items.length ? h('div', { class: 'card' },
        h('h3', {}, `追加予定の動画 (${s.to_add} 本、古い譜面から順に追加)`),
        h('table', { class: 'table' }, h('tbody', {}, items.map((it, i) => h('tr', {},
          h('td', { class: 'mono muted small' }, String(i + 1)),
          h('td', {}, h('a', { href: `#/song/${enc(it.set_key)}` }, it.title),
            it.set_count > 1 ? h('span', { class: 'tag' }, `${it.set_count} 譜面`) : null,
            h('div', { class: 'muted small' }, it.artist)),
          h('td', { class: 'mono small' }, h('a', {
            href: `https://www.youtube.com/watch?v=${enc(it.video_id)}`, target: '_blank', rel: 'noopener',
          }, it.video_id)))))),
        data.items.length > SHOW_ITEMS && !showAll
          ? h('button', { class: 'btn small ghost', onclick: () => { showAll = true; draw(); } }, `すべて表示 (${data.items.length})`) : null,
        s.to_add > data.items.length ? h('p', { class: 'muted small' }, `ほか ${s.to_add - data.items.length} 本`) : null,
      ) : null,
    );
  }

  draw();
  const offJob = onJobFinished(j => {
    if (['sync', 'match', 'scan'].includes(j.kind)) reload().catch(err => toast(err.message, 'error'));
  });
  let wasRunning = running();
  const offStatus = onStatus(() => { if (running() !== wasRunning) { wasRunning = running(); draw(); } });
  return () => { offJob(); offStatus(); };
}
