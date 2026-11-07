// Settings: osu!lazer path & environment, matching jobs, Google login, playlist, advanced.
import { api } from '../api.js';
import { store, refreshStatus, onStatus, startJob } from '../app.js';
import { h, toast, fmtTime, busy, fill } from '../util.js';

const PRIVACY = [['private', '非公開'], ['unlisted', '限定公開'], ['public', '公開']];

export async function render(root) {
  let settings = await api('/settings');
  let pls = await api('/playlists');
  let loginPoll = null;

  const sec = {
    lazer: h('section', { class: 'card' }),
    match: h('section', { class: 'card' }),
    google: h('section', { class: 'card' }),
    playlist: h('section', { class: 'card' }),
    advanced: h('section', { class: 'card' }),
  };
  fill(root, h('div', { class: 'settings' }, Object.values(sec)));

  function check(ok, label, detail) {
    return h('li', { class: ok ? 'ok' : 'ng' }, h('span', { class: 'mark' }, ok ? '✓' : '✕'), label,
      detail ? h('span', { class: 'muted small' }, ` ${detail}`) : null);
  }

  // Unsaved edits survive redraws triggered by status polling.
  const draft = {};
  const keep = (name, el) => { el.addEventListener('input', () => { draft[name] = el.value; }); return el; };

  function drawLazer(st) {
    const env = st.env;
    const path = keep('lazer_path', h('input', { class: 'input wide', value: draft.lazer_path ?? settings.lazer_path, spellcheck: 'false' }));
    const save = h('button', {
      class: 'btn', onclick: async () => {
        const done = busy(save);
        try { settings = await api('/settings', { method: 'PUT', body: { lazer_path: path.value.trim() } }); delete draft.lazer_path; toast('保存しました', 'ok'); refreshStatus(); }
        catch (e) { toast(e.message, 'error', 6000); }
        done();
      },
    }, '保存');
    const ls = st.last_scan;
    fill(sec.lazer, 
      h('h2', {}, 'osu!lazer ライブラリ'),
      h('label', { class: 'field' }, 'データフォルダ (client.realm がある場所)', h('div', { class: 'row' }, path, save)),
      h('ul', { class: 'checks' },
        check(env.realm_found, 'client.realm'),
        check(!!env.dotnet, '.NET SDK', env.dotnet || 'winget install Microsoft.DotNet.SDK.9'),
        check(env.helper_built, '読み取りヘルパー', env.helper_built ? 'ビルド済み' : '初回スキャン時に自動ビルド'),
        check(!!env.ffprobe, 'ffprobe', env.ffprobe ? '' : '無くても動きますが曲の長さが不正確になります'),
        check(true, 'yt-dlp', env.yt_dlp)),
      h('p', { class: 'muted small' }, ls
        ? `前回のスキャン: ${fmtTime(ls.started_at)} — ${ls.status === 'error' ? 'エラー: ' + ls.error : `${ls.sets_total ?? '?'} セット (新規 ${ls.sets_new ?? 0})`}`
        : 'まだスキャンしていません'),
      h('div', { class: 'btnrow' },
        h('button', { class: 'btn primary', onclick: () => startJob('/library/scan', { force: false }, 'スキャン') }, 'スキャン'),
        h('button', { class: 'btn', onclick: () => startJob('/library/scan', { force: true }, 'スキャン') }, '強制スキャン'),
        h('span', { class: 'muted small' }, 'client.realm が前回から変わっていなければ通常スキャンはスキップされます')),
    );
  }

  function drawMatch() {
    fill(sec.match, 
      h('h2', {}, 'YouTube マッチング'),
      h('p', { class: 'muted small' }, '検索は yt-dlp で行うため API クォータは消費しません。1 曲あたり数秒かかります。'),
      h('div', { class: 'btnrow' },
        h('button', { class: 'btn primary', onclick: () => startJob('/match', { scope: 'unmatched' }, 'マッチング') }, '未マッチの曲を検索'),
        h('button', { class: 'btn', onclick: () => startJob('/match', { scope: 'needs_review' }, '要確認の再検索') }, '要確認の曲を再検索'),
        h('button', {
          class: 'btn', title: '保存済みの候補を今の採点ルールで判定し直します (検索しない)',
          onclick: async e => {
            const done = busy(e.target);
            try { const r = await api('/match/rescore', { method: 'POST' }); toast(`再採点: 確定 ${r.confirmed} / 要確認 ${r.needs_review}`, 'ok'); }
            catch (err) { toast(err.message, 'error'); }
            done();
          },
        }, '再採点')),
      h('p', { class: 'muted small' }, '手動で選んだ動画は再検索・再採点で上書きされません。'),
    );
  }

  function drawGoogle(st) {
    const a = st.auth;
    const login = h('button', { class: 'btn primary', onclick: startLogin }, a.logged_in ? '別のアカウントでログイン' : 'Google でログイン');
    fill(sec.google, 
      h('h2', {}, 'YouTube アカウント'),
      h('ul', { class: 'checks' },
        check(a.client_secret, 'client_secret.json', a.client_secret ? '' : a.client_secret_message),
        check(a.logged_in, a.logged_in ? 'ログイン済み' : '未ログイン', a.message || '')),
      h('div', { class: 'btnrow' },
        a.client_secret ? login : null,
        a.logged_in ? h('button', {
          class: 'btn ghost', onclick: async () => {
            if (!confirm('ログアウトしてトークンを削除します。よろしいですか?')) return;
            await api('/auth/logout', { method: 'POST' }); toast('ログアウトしました'); refreshStatus();
          },
        }, 'ログアウト') : null),
      h('p', { class: 'muted small' },
        'ログインするとブラウザに Google の画面が開きます。「このアプリは確認されていません」と出たら「続行」を選んでください。設定手順は README を参照。'),
    );
  }

  async function startLogin(e) {
    const done = busy(e.target, 'ブラウザで許可してください…');
    try { await api('/auth/login', { method: 'POST' }); }
    catch (err) { toast(err.message, 'error', 8000); done(); return; }
    clearInterval(loginPoll);
    loginPoll = setInterval(async () => {
      const s = await api('/auth');
      if (s.login === 'waiting') return;
      clearInterval(loginPoll);
      done();
      if (s.login === 'done') { toast('ログインしました', 'ok'); refreshStatus(); }
      else toast(`ログインに失敗しました: ${s.error}`, 'error', 8000);
    }, 1500);
  }

  function drawPlaylist(st) {
    const loggedIn = st.auth.logged_in;
    const active = pls.active;
    const title = keep('pl_title', h('input', { class: 'input', value: draft.pl_title ?? 'osu! songs', maxlength: '150' }));
    const privacy = h('select', { class: 'input' }, PRIVACY.map(([v, l]) => h('option', { value: v }, l)));
    const list = pls.playlists.length
      ? h('table', { class: 'table' }, h('tbody', {}, pls.playlists.map(p => h('tr', { class: p.id === active ? 'active' : '' },
        h('td', {}, h('input', {
          type: 'radio', name: 'pl', checked: p.id === active,
          onchange: async () => { pls = await api('/playlists/active', { method: 'PUT', body: { id: p.id } }); toast(`「${p.title}」を使います`, 'ok'); refreshStatus(); redraw(); },
        })),
        h('td', {}, h('a', { href: `https://www.youtube.com/playlist?list=${encodeURIComponent(p.id)}`, target: '_blank', rel: 'noopener' }, p.title || p.id)),
        h('td', { class: 'muted small' }, { private: '非公開', unlisted: '限定公開', public: '公開' }[p.privacy] || p.privacy || ''),
        h('td', { class: 'muted small mono' }, p.item_count != null ? `${p.item_count} 件` : ''),
        h('td', { class: 'muted small' }, p.created_by_tool ? 'osu2yt で作成' : '')))))
      : h('p', { class: 'muted' }, 'プレイリスト情報がありません。「一覧を更新」で YouTube から取得します。');

    const call = (label, path, fmt) => async e => {
      const done = busy(e.target);
      try { const r = await api(path, { method: 'POST' }); if (fmt) toast(fmt(r), 'ok', 6000); pls = await api('/playlists'); refreshStatus(); redraw(); }
      catch (err) { toast(err.message, 'error', 8000); }
      done();
    };

    fill(sec.playlist, 
      h('h2', {}, 'プレイリスト'),
      !loggedIn ? h('p', { class: 'muted' }, '先に YouTube アカウントでログインしてください。') : null,
      list,
      h('div', { class: 'btnrow' },
        h('button', { class: 'btn', disabled: !loggedIn, onclick: call('更新', '/playlists/refresh') }, '一覧を更新 (1 unit)'),
        h('button', {
          class: 'btn', disabled: !loggedIn || !active,
          onclick: call('取得', '/playlists/pull', r => `中身を取得: ${r.total} 件 (新たに見つかった ${r.new_remote} / 消えていた ${r.went_missing})`),
        }, '選択中の中身を取得'),
        h('button', {
          class: 'btn', disabled: !loggedIn,
          onclick: call('確認', '/videos/check', r => `生存確認 ${r.checked} 本: 再生不可 ${Object.keys(r.unavailable).length} 本 (要確認に戻した曲 ${r.sets_flagged})`),
        }, '採用動画の生存確認')),
      h('h3', {}, '新しく作成'),
      h('form', {
        class: 'row',
        onsubmit: async e => {
          e.preventDefault();
          if (!confirm(`プレイリスト「${title.value}」(${privacy.selectedOptions[0].textContent}) を作成します。クォータを 50 unit 消費します。`)) return;
          const btn = e.target.querySelector('button'); const done = busy(btn);
          try { pls = await api('/playlists', { method: 'POST', body: { title: title.value, privacy: privacy.value } }); delete draft.pl_title; toast('作成しました', 'ok'); refreshStatus(); redraw(); }
          catch (err) { toast(err.message, 'error', 8000); }
          done();
        },
      }, title, privacy, h('button', { class: 'btn', type: 'submit', disabled: !loggedIn }, '作成 (50 unit)')),
    );
  }

  function drawAdvanced() {
    const region = h('input', { class: 'input narrow', value: settings.region, maxlength: '2' });
    const limit = h('input', { class: 'input narrow', type: 'number', value: settings.quota_limit, min: '100' });
    const margin = h('input', { class: 'input narrow', type: 'number', value: settings.quota_margin, min: '0' });
    const prot = h('input', { type: 'checkbox', checked: settings.include_protected === '1' });
    const resume = h('input', { type: 'checkbox', checked: settings.auto_resume !== '0' });
    fill(sec.advanced, 
      h('h2', {}, '詳細設定'),
      h('div', { class: 'grid2' },
        h('label', { class: 'field' }, '地域 (再生可否の判定)', region),
        h('label', { class: 'field' }, '1 日のクォータ上限', limit),
        h('label', { class: 'field' }, '残しておく余裕 (unit)', margin),
        h('label', { class: 'check' }, prot, 'lazer 同梱の譜面も含める (次回スキャンから)'),
        h('label', { class: 'check' }, resume, 'クォータで一時停止した同期をリセット後に自動で再開')),
      h('button', {
        class: 'btn', onclick: async e => {
          const done = busy(e.target);
          try {
            settings = await api('/settings', { method: 'PUT', body: {
              region: region.value.trim().toUpperCase(), quota_limit: Number(limit.value),
              quota_margin: Number(margin.value), include_protected: prot.checked, auto_resume: resume.checked,
            } });
            toast('保存しました', 'ok'); refreshStatus();
          } catch (err) { toast(err.message, 'error', 6000); }
          done();
        },
      }, '保存'),
    );
  }

  // Polling delivers a status every few seconds; only rebuild when something shown here changed.
  let lastSig = null;
  function redraw(force = true) {
    const st = store.status;
    if (!st) return;
    const sig = JSON.stringify([st.env, st.last_scan, st.auth, st.playlist, st.job?.id, st.job?.status]);
    if (!force && sig === lastSig) return;
    lastSig = sig;
    drawLazer(st); drawGoogle(st); drawPlaylist(st);
  }

  drawMatch();
  drawAdvanced();
  redraw();
  const editing = () => document.activeElement?.matches?.('section input, section select, section textarea');
  const off = onStatus(() => { if (!editing()) redraw(false); });
  return () => { off(); clearInterval(loginPoll); };
}
