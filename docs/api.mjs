const SESSION_KEY = 'short_bot_dashboard_session_v1';

const json = async response => {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data?.msg || data?.message || data?.error_description || data?.error || `HTTP ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
};

const requireConfig = config => {
  if (!config?.supabaseUrl || !config?.supabasePublishableKey) {
    throw new Error('Supabase 設定不完整。');
  }
};

export function loadSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function saveSession(session) {
  localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  return session;
}

export function clearSession() {
  localStorage.removeItem(SESSION_KEY);
}

export async function signIn(config, email, password) {
  requireConfig(config);
  const response = await fetch(
    new URL('/auth/v1/token?grant_type=password', config.supabaseUrl),
    {
      method: 'POST',
      headers: {
        apikey: config.supabasePublishableKey,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ email, password }),
    },
  );
  const data = await json(response);
  if (data?.user?.app_metadata?.short_bot_role !== 'owner') {
    throw new Error('此帳號沒有 Short Bot Dashboard 權限。');
  }
  return saveSession({
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + Number(data.expires_in || 3600),
    user: data.user,
  });
}

export async function refreshSession(config, session = loadSession()) {
  requireConfig(config);
  if (!session?.refresh_token) throw new Error('尚未登入。');
  const now = Math.floor(Date.now() / 1000);
  if (session.access_token && Number(session.expires_at || 0) > now + 90) return session;

  const response = await fetch(
    new URL('/auth/v1/token?grant_type=refresh_token', config.supabaseUrl),
    {
      method: 'POST',
      headers: {
        apikey: config.supabasePublishableKey,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ refresh_token: session.refresh_token }),
    },
  );
  const data = await json(response);
  if (data?.user?.app_metadata?.short_bot_role !== 'owner') {
    clearSession();
    throw new Error('此帳號沒有 Short Bot Dashboard 權限。');
  }
  return saveSession({
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + Number(data.expires_in || 3600),
    user: data.user,
  });
}

export async function signOut(config, session = loadSession()) {
  try {
    if (session?.access_token) {
      await fetch(new URL('/auth/v1/logout', config.supabaseUrl), {
        method: 'POST',
        headers: {
          apikey: config.supabasePublishableKey,
          Authorization: `Bearer ${session.access_token}`,
        },
      });
    }
  } finally {
    clearSession();
  }
}

async function supabase(config, session, path, options = {}) {
  const active = await refreshSession(config, session);
  const headers = {
    apikey: config.supabasePublishableKey,
    Authorization: `Bearer ${active.access_token}`,
    Accept: 'application/json',
    ...(options.headers || {}),
  };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(new URL(`/rest/v1/${path}`, config.supabaseUrl), {
    ...options,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: 'no-store',
  });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!response.ok) {
    const message = data?.message || data?.details || data?.hint || `HTTP ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return { data, session: active, response };
}

const toDate = value => value instanceof Date ? value : new Date(`${value}T12:00:00+08:00`);
const dateString = value => {
  const d = value instanceof Date ? value : toDate(value);
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(d);
};
export const offsetDate = (date, days) => dateString(new Date(toDate(date).getTime() + days * 86400000));

async function fetchLive(config, session) {
  if (!config?.renderUrl) return null;
  const active = await refreshSession(config, session);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(new URL('/dashboard-live', config.renderUrl), {
      headers: { Authorization: `Bearer ${active.access_token}` },
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`Render live feed HTTP ${response.status}`);
    return { data: await response.json(), session: active };
  } finally {
    clearTimeout(timer);
  }
}

async function syncLiveRows(config, session, live) {
  let active = session;
  if (Array.isArray(live?.candidates) && live.candidates.length) {
    const result = await supabase(
      config, active,
      'dashboard_candidates?on_conflict=scan_date,strategy,code',
      {
        method: 'POST',
        headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
        body: live.candidates,
      },
    );
    active = result.session;
  }
  if (Array.isArray(live?.alerts) && live.alerts.length) {
    const result = await supabase(
      config, active,
      'dashboard_alerts?on_conflict=id',
      {
        method: 'POST',
        headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
        body: live.alerts,
      },
    );
    active = result.session;
  }
  return active;
}

export async function loadDashboard(config, date, session = loadSession()) {
  let active = await refreshSession(config, session);
  let live = null;
  let liveError = '';

  try {
    const result = await fetchLive(config, active);
    live = result?.data || null;
    active = result?.session || active;
    active = await syncLiveRows(config, active, live);
  } catch (error) {
    liveError = error?.name === 'AbortError'
      ? 'Render 即時資料逾時'
      : (error?.message || 'Render 即時資料讀取失敗');
  }

  const start = offsetDate(date, -89);
  const enc = encodeURIComponent;
  const candidatesPath =
    `dashboard_candidates?select=scan_date,scan_time,code,name,strategy,price,change,setup,chip,score,status,payload,updated_at&scan_date=gte.${enc(start)}&scan_date=lte.${enc(date)}&order=scan_date.desc,scan_time.desc`;
  const alertsPath =
    `dashboard_alerts?select=id,scan_date,alert_time,code,name,strategy,entry,stop,target,score,grade,setup,reentry,payload,created_at&scan_date=gte.${enc(start)}&scan_date=lte.${enc(date)}&order=scan_date.desc,alert_time.desc`;
  const tradesPath =
    `dashboard_trades?select=id,date,time,exit_date,exit_time,code,name,strategy,status,entry,exit,quantity,costs,pnl,note,created_at,updated_at&date=gte.${enc(start)}&date=lte.${enc(date)}&order=date.desc,time.desc`;
  const experimentsPath =
    `dashboard_experiments?select=id,date,strategy,name,description,status,samples,win_rate,pnl,created_at&date=gte.${enc(start)}&date=lte.${enc(date)}&order=date.desc,created_at.desc`;

  const c = await supabase(config, active, candidatesPath);
  active = c.session;
  const a = await supabase(config, active, alertsPath);
  active = a.session;
  const t = await supabase(config, active, tradesPath);
  active = t.session;
  const e = await supabase(config, active, experimentsPath);
  active = e.session;

  const candidates = (c.data || []).map(row => ({
    date: row.scan_date,
    time: row.scan_time,
    code: row.code,
    name: row.name,
    strategy: row.strategy,
    price: Number(row.price),
    change: Number(row.change),
    setup: row.setup,
    chip: row.chip,
    score: Number(row.score),
    status: row.status,
    payload: row.payload || {},
  }));
  const alerts = (a.data || []).map(row => ({
    id: row.id,
    date: row.scan_date,
    time: row.alert_time,
    code: row.code,
    name: row.name,
    strategy: row.strategy,
    entry: Number(row.entry),
    stop: Number(row.stop),
    target: Number(row.target),
    score: Number(row.score),
    grade: row.grade,
    setup: row.setup,
    reentry: Boolean(row.reentry),
    payload: row.payload || {},
  }));
  const trades = (t.data || []).map(row => ({
    ...row,
    entry: Number(row.entry),
    exit: row.exit === null ? null : Number(row.exit),
    quantity: Number(row.quantity),
    costs: Number(row.costs),
    pnl: row.pnl === null ? null : Number(row.pnl),
  }));
  const experiments = (e.data || []).map(row => ({
    ...row,
    samples: Number(row.samples),
    win_rate: row.win_rate === null ? null : Number(row.win_rate),
    pnl: Number(row.pnl),
  }));

  return {
    session: active,
    live,
    liveError,
    snapshot: {
      schema_version: 1,
      generated_at: live?.generated_at || new Date().toISOString(),
      candidates,
      trades,
      experiments,
    },
    alerts,
    runtime: live?.runtime || null,
  };
}

export async function saveTrade(config, input, session = loadSession()) {
  let active = await refreshSession(config, session);
  const payload = {
    date: input.date,
    time: input.time,
    code: String(input.code || '').trim(),
    name: String(input.name || '').trim(),
    strategy: 'V2.9',
    status: input.status,
    entry: Number(input.entry),
    exit: input.status === 'closed' ? Number(input.exit) : null,
    exit_date: input.status === 'closed' ? (input.exit_date || input.date) : null,
    exit_time: input.status === 'closed' ? (input.exit_time || input.time) : null,
    quantity: Number(input.quantity),
    costs: Number(input.costs || 0),
    note: String(input.note || '').trim(),
    updated_at: new Date().toISOString(),
  };
  if (!payload.date || !/^\d{2}:\d{2}$/.test(payload.time)) throw new Error('請確認進場日期與時間。');
  if (!payload.code || !payload.name) throw new Error('請填寫股票代號與名稱。');
  if (!(payload.entry > 0) || !(payload.quantity > 0)) throw new Error('進場價與股數必須大於 0。');
  if (payload.status === 'closed' && !(payload.exit > 0)) throw new Error('平倉交易需要出場價。');

  if (input.id) {
    const result = await supabase(
      config, active,
      `dashboard_trades?id=eq.${encodeURIComponent(input.id)}`,
      { method: 'PATCH', headers: { Prefer: 'return=representation' }, body: payload },
    );
    return { session: result.session, trade: Array.isArray(result.data) ? result.data[0] : result.data };
  }

  const result = await supabase(
    config, active,
    'dashboard_trades',
    { method: 'POST', headers: { Prefer: 'return=representation' }, body: payload },
  );
  return { session: result.session, trade: Array.isArray(result.data) ? result.data[0] : result.data };
}

export async function deleteTrade(config, id, session = loadSession()) {
  const result = await supabase(
    config, session,
    `dashboard_trades?id=eq.${encodeURIComponent(id)}`,
    { method: 'DELETE', headers: { Prefer: 'return=minimal' } },
  );
  return result.session;
}
