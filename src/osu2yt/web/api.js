// JSON API wrapper. Mutating requests carry X-Osu2yt (the server rejects them otherwise: CSRF guard).

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = 'GET', body, query } = {}) {
  let url = '/api' + path;
  if (query) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === '' || v === false) continue;
      p.set(k, v === true ? 'true' : v);
    }
    const s = p.toString();
    if (s) url += '?' + s;
  }
  const opts = { method, headers: {} };
  if (method !== 'GET') opts.headers['X-Osu2yt'] = '1';
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    throw new ApiError('サーバーに接続できません (osu2yt が終了していませんか?)', 0);
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    if (data && data.detail) {
      msg = typeof data.detail === 'string'
        ? data.detail
        : data.detail.map?.(d => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join(', ') || JSON.stringify(data.detail);
    }
    throw new ApiError(msg, res.status);
  }
  return data;
}

export const enc = encodeURIComponent;
