import {
  taipeiDate,
  escapeHTML as esc,
  selectTrades,
  summarize,
  cumulativeSeries,
  csv,
  emptySnapshot,
  offsetDate,
} from './data.mjs';
import {
  loadSession,
  signIn,
  signOut,
  loadDashboard,
  saveTrade,
  deleteTrade,
} from './api.mjs';

const $ = selector => document.querySelector(selector);
const config = window.SHORT_BOT_CONFIG ?? {};
const titles = {
  today: ['今日監控', 'Bot 觀察與提醒自動同步；實際成交由你自行記錄。'],
  performance: ['策略績效', '只用你記錄且已平倉的實際成交，計算策略表現與風險。'],
  smart: ['Smart Entry Lab', '把每次 Bot 訊號的預期進場先鎖定，再長期追蹤成交率、MFE、MAE、停損與 2R。'],
  trades: ['交易明細', '新增、編輯與平倉你的實際成交，不把 Bot 紙上提醒混入績效。'],
  shadow: ['Shadow Lab', '模擬實驗獨立呈現，不混入正式實際成交績效。'],
};

let snapshot = emptySnapshot();
let alerts = [];
let smartEntries = [];
let runtime = null;
let liveError = '';
let session = loadSession();
let page = 'today';
let days = 30;
let status = 'all';
let query = '';
let loading = false;

$('#date').value = taipeiDate();
$('#login-email').value = config.ownerEmail || '';

const money = value => value === null || value === undefined
  ? '—'
  : new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(value);
const signed = value => value === null || value === undefined ? '—' : `${value > 0 ? '+' : ''}${money(value)}`;
const percent = value => value === null || value === undefined ? '—' : `${Number(value).toFixed(1)}%`;
const signClass = value => value > 0 ? 'positive' : value < 0 ? 'negative' : 'muted-text';
const current = () => ({ date: $('#date').value || taipeiDate(), strategy: $('#strategy').value });

function metric(label, value, note, style = '') {
  return `<article class="metric"><div class="metric-label">${label}<span>↗</span></div><div class="metric-value ${style}">${value}</div><div class="metric-note">${note}</div></article>`;
}

function empty(cols, text) {
  return `<tr><td colspan="${cols}" class="empty-row">${text}</td></tr>`;
}

function setAuthVisible(showLogin) {
  $('#auth-screen').hidden = !showLogin;
  $('#app-root').hidden = showLogin;
}

function showLogin(message = '') {
  setAuthVisible(true);
  $('#login-error').textContent = message;
  $('#login-password').value = '';
}

function renderStatus() {
  const connected = Boolean(session && snapshot);
  $('#source-badge').textContent = runtime ? 'Bot + Supabase' : connected ? 'Supabase' : '未連線';
  $('#source-badge').className = 'badge';
  $('#bot-dot').className = `dot ${runtime ? '' : 'muted'}`;
  $('#bot-status').textContent = runtime
    ? `已連線 · ${runtime.last_precise_scan ? '最近掃描 ' + new Date(runtime.last_precise_scan).toLocaleTimeString('zh-TW', { timeZone: 'Asia/Taipei', hour12: false }) : '等待盤中掃描'}`
    : '即時狀態未取得';

  const notice = $('#notice');
  notice.className = `notice${liveError ? ' warn' : ''}`;
  notice.textContent = liveError
    ? `◈ Supabase 已連線；${liveError}。已保留資料庫中的歷史觀察、提醒與實際成交。`
    : '◈ Bot 觀察／提醒會從 Render 自動讀取並同步到 Supabase；績效只計算你記錄的已平倉實際成交。';

  $('#updated').textContent = snapshot.generated_at
    ? `更新 ${new Date(snapshot.generated_at).toLocaleString('zh-TW', { timeZone: 'Asia/Taipei', hour12: false })}`
    : '尚未同步資料';
}

function chart(trades, period) {
  if (!trades.some(t => t.status === 'closed')) {
    return '<div class="chart-empty"><span>⌁</span>尚無已平倉實際成交<small>完成平倉後會顯示累積淨損益曲線</small></div>';
  }
  const points = cumulativeSeries(trades, current().date, period);
  const vals = [0, ...points.map(p => p.value)];
  const lo = Math.min(...vals), hi = Math.max(...vals), range = hi - lo || 1;
  const x = i => 54 + i / (points.length - 1 || 1) * 646;
  const y = v => 185 - (v - lo) / range * 155;
  const line = points.map((p, i) => `${x(i)},${y(p.value)}`).join(' ');
  const grid = [0, 1, 2, 3].map(i => {
    const value = lo + range * i / 3;
    return `<line x1="54" x2="715" y1="${y(value)}" y2="${y(value)}" stroke="#28343f" stroke-dasharray="3 5"/><text x="0" y="${y(value) + 3}" fill="#708598" font-size="10">${money(value)}</text>`;
  }).join('');
  return `<div class="chart"><div class="chart-legend"><span><i class="dot"></i>累積已實現淨損益 · TWD</span><span>區間起點歸零</span></div><svg viewBox="0 0 740 220" role="img" aria-label="${period} 日累積已實現淨損益"><defs><linearGradient id="fade" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#83e7bf" stop-opacity=".19"/><stop offset="1" stop-color="#83e7bf" stop-opacity="0"/></linearGradient></defs>${grid}<polygon points="54,185 ${line} 700,185" fill="url(#fade)"/><polyline points="${line}" stroke="#83e7bf" stroke-width="2.5" fill="none"/>${points.filter((_, i) => i === 0 || i === Math.floor(period / 2) || i === period - 1).map(p => `<text x="${x(points.indexOf(p))}" y="211" fill="#708598" font-size="10" text-anchor="middle">${p.date.slice(5)}</text>`).join('')}</svg></div>`;
}

function candidateTable(rows, dayAlerts) {
  const alerted = new Set(dayAlerts.map(a => a.code));
  return `<div class="panel"><div class="panel-head"><div><h2>Bot 策略觀察清單 <span class="count">${rows.length}</span></h2><p>觀察日期 · V2.9</p></div><span>自動同步</span></div><div class="table-wrap"><table><thead><tr><th>股票</th><th>參考價</th><th>漲跌幅</th><th>型態</th><th>二日籌碼</th><th>評分</th><th>狀態</th></tr></thead><tbody>${rows.length ? rows.map(c => `<tr><td><strong>${esc(c.code)}</strong><small>${esc(c.name)}</small></td><td>${Number(c.price).toFixed(2)}</td><td class="${signClass(c.change)}">${c.change > 0 ? '+' : ''}${Number(c.change).toFixed(2)}%</td><td>${esc(c.setup)}</td><td>${esc(c.chip)}</td><td>${Number(c.score).toFixed(0)} <span class="muted-text">分</span></td><td><span class="chip ${alerted.has(c.code) ? '' : 'wait'}">${alerted.has(c.code) ? '已提醒' : esc(c.status)}</span></td></tr>`).join('') : empty(7, '此日期尚無 Bot 觀察清單')}</tbody></table></div><div class="panel-bottom"><span>Bot 提醒不是實際成交</span><a href="#trades">記錄實際交易 ↗</a></div></div>`;
}

function alertTable(rows) {
  return `<div class="panel alert-panel"><div class="panel-head"><div><h2>Bot 提醒紀錄 <span class="count">${rows.length}</span></h2><p>只代表策略當時發出候選提醒</p></div><span>不計入正式績效</span></div><div class="table-wrap"><table><thead><tr><th>時間</th><th>股票</th><th>型態</th><th>參考進場</th><th>停損</th><th>2R</th><th>等級 / 分數</th></tr></thead><tbody>${rows.length ? rows.map(a => `<tr><td>${esc(a.time)}</td><td><strong>${esc(a.code)}</strong><small>${esc(a.name)}</small></td><td>${esc(a.setup)}${a.reentry ? '<small>二次進場</small>' : ''}</td><td>${Number(a.entry).toFixed(2)}</td><td>${Number(a.stop).toFixed(2)}</td><td>${Number(a.target).toFixed(2)}</td><td><span class="chip">${esc(a.grade || '—')} · ${Number(a.score).toFixed(0)}</span></td></tr>`).join('') : empty(7, '此日期尚無 Bot 盤中提醒')}</tbody></table></div></div>`;
}

function todayView() {
  const { date, strategy } = current();
  const trades = selectTrades(snapshot, { date, strategy });
  const dayAlerts = alerts.filter(a => a.date === date && (strategy === 'all' || a.strategy === strategy));
  const s = summarize(trades);
  const rows = snapshot.candidates.filter(c => c.date === date && (strategy === 'all' || c.strategy === strategy));
  const actualCount = trades.length;
  return `<div class="metrics">${metric('監控標的', rows.length, 'Bot 選定日期觀察清單')}${metric('Bot 提醒', dayAlerts.length, '策略提醒，不等於實際成交')}${metric('已實現淨損益', s.closed ? signed(s.pnl) : '—', `TWD · ${actualCount} 筆實際成交`, signClass(s.pnl))}${metric('已平倉勝率', percent(s.winRate), s.closed ? `${s.wins} 勝 / ${s.closed} 筆已平倉` : '等待實際平倉資料')}</div><div class="grid-main"><article class="panel"><div class="panel-head"><div><h2>實際成交損益走勢</h2><p>近 14 日 · 僅計已平倉實際成交</p></div><span class="chip">ACTUAL</span></div>${chart(selectTrades(snapshot, { date, strategy, days: 14 }), 14)}</article><article class="panel rules-panel"><div class="panel-head"><h2>策略運行設定</h2><span>V2.9</span></div><div class="rule-list"><div class="rule"><span>09:00 — 09:15</span><b>每 30 秒掃描</b></div><div class="rule"><span>09:15 — 10:00</span><b>每 60 秒掃描</b></div><div class="rule"><span>10:00 — 11:30</span><b>每 120 秒掃描</b></div><div class="rule"><span>資料日期</span><b class="positive">${runtime?.candidate_scan_date || '依交易日'}</b></div></div><div class="callout">${runtime ? 'Render Bot 已連線。Dashboard 只讀 Bot 訊號，不會下單或更動策略。' : 'Render 即時狀態未取得；Supabase 歷史資料仍可使用。'}</div></article></div>${candidateTable(rows, dayAlerts)}${alertTable(dayAlerts)}`;
}

function performanceView() {
  const trades = selectTrades(snapshot, { ...current(), days });
  const s = summarize(trades);
  return `<div class="metrics">${metric('區間淨損益', s.closed ? signed(s.pnl) : '—', `近 ${days} 日 · TWD`, signClass(s.pnl))}${metric('已平倉勝率', percent(s.winRate), `${s.closed} 筆已平倉實際成交`)}${metric('獲利因子', s.profitFactor === null ? '—' : s.profitFactor === Infinity ? '∞' : s.profitFactor.toFixed(2), '獲利總額 ÷ 虧損絕對值')}${metric('最大回撤', s.closed ? money(s.maxDrawdown) : '—', 'TWD · 區間累積損益峰谷差')}</div><article class="panel"><div class="panel-head"><div><h2>累積已實現淨損益</h2><p>以實際進場日期分組 · 不含 Bot 紙上提醒與未實現損益</p></div><div class="period" aria-label="績效期間">${[7, 30, 90].map(d => `<button data-days="${d}" class="${days === d ? 'active' : ''}" aria-pressed="${days === d}">${d} 天</button>`).join('')}</div></div>${chart(trades, days)}</article><div class="panel wide-note"><span>⌁</span><div><strong>正式績效與 Shadow 分開</strong>淨損益由實際進出場價、股數與你填寫的交易成本計算。未平倉交易不會提前算進績效。</div></div>`;
}


function smartStatus(item) {
  const statusMap = {
    pending: ['等待成交', 'wait'],
    filled: ['已成交追蹤', ''],
    expired: ['未成交過期', 'closed'],
    invalidated: ['進場前失效', 'closed'],
    completed: ['追蹤完成', ''],
  };
  return statusMap[item.status] || [item.status || '—', 'closed'];
}

function smartOutcome(item) {
  const map = {
    '2r_first': '先到 2R',
    'stop_first': '先碰停損',
    'window_end': '60 分鐘結束',
    'not_filled': '未成交',
    'stop_before_fill': '進場前碰停損',
  };
  return map[item.first_event] || (item.status === 'filled' ? '追蹤中' : '—');
}

function smartView() {
  const { date, strategy } = current();
  const start = offsetDate(date, 1 - days);
  const rows = smartEntries.filter(item =>
    item.scan_date >= start &&
    item.scan_date <= date &&
    (strategy === 'all' || item.strategy === strategy)
  );
  const filled = rows.filter(item => item.fill_price !== null && item.fill_price !== undefined);
  const baseline2r = rows.filter(item => item.baseline_hit_2r).length;
  const smart2r = rows.filter(item => item.hit_2r).length;
  const baselineStopFirst = rows.filter(item => item.baseline_first_event === 'stop_first').length;
  const smartStopFirst = rows.filter(item => item.first_event === 'stop_first').length;
  const fillRate = rows.length ? filled.length / rows.length * 100 : null;
  const baseline2rRate = rows.length ? baseline2r / rows.length * 100 : null;
  const smart2rEffectiveRate = rows.length ? smart2r / rows.length * 100 : null;
  const delta = baseline2rRate === null || smart2rEffectiveRate === null ? null : smart2rEffectiveRate - baseline2rRate;
  const avg = (list, field) => {
    const values = list.map(item => item[field]).filter(value => value !== null && value !== undefined && Number.isFinite(Number(value)));
    return values.length ? values.reduce((sum, value) => sum + Number(value), 0) / values.length : null;
  };
  const smartMfe = avg(filled, 'mfe_pct');
  const smartMae = avg(filled, 'mae_pct');
  const baseMfe = avg(rows, 'baseline_mfe_pct');
  const baseMae = avg(rows, 'baseline_mae_pct');

  return `<div class="metrics">
    ${metric('V2.9 基準 2R', percent(baseline2rRate), `${baseline2r} / ${rows.length} 筆訊號`)}
    ${metric('SMART_V1 有效 2R', percent(smart2rEffectiveRate), `${smart2r} / ${rows.length} 筆原始訊號`, signClass(delta))}
    ${metric('預期成交率', percent(fillRate), `${filled.length} / ${rows.length} 筆等到預期限價`)}
    ${metric('Smart - 基準', delta === null ? '—' : `${delta > 0 ? '+' : ''}${delta.toFixed(1)}%`, '2R 命中率差；樣本少時先不要下結論', signClass(delta))}
  </div>
  <article class="panel smart-rule-card">
    <div class="panel-head">
      <div><h2>SMART_V1 預期進場規則</h2><p>原 V2.9 訊號與 Smart Entry 同時鎖定，兩條路徑都保存，避免事後改答案</p></div>
      <div class="period" aria-label="Smart Entry 期間">${[7,30,90].map(d => `<button data-days="${d}" class="${days===d?'active':''}" aria-pressed="${days===d}">${d} 天</button>`).join('')}</div>
    </div>
    <div class="smart-rule-grid">
      <div><span>基準路徑</span><strong>V2.9 訊號價直接進</strong><small>用原結構停損，追蹤 1R / 2R、MFE / MAE</small></div>
      <div><span>Smart 預期限價</span><strong>訊號價上方 1 檔</strong><small>可接受區間為訊號價～上方 2 檔，且不得跨過停損</small></div>
      <div><span>Smart 有效時間</span><strong>20 分鐘</strong><small>沒有等到預期價就標記未成交，不追價</small></div>
      <div><span>觀察窗口</span><strong>60 分鐘</strong><small>同時記錄 5 / 15 / 30 / 60 分鐘與路徑極值</small></div>
    </div>
    <div class="smart-compare-strip">
      <span>基準平均 MFE <b>${baseMfe === null ? '—' : baseMfe.toFixed(2)+'%'}</b> / MAE <b>${baseMae === null ? '—' : baseMae.toFixed(2)+'%'}</b></span>
      <span>Smart 成交後平均 MFE <b>${smartMfe === null ? '—' : smartMfe.toFixed(2)+'%'}</b> / MAE <b>${smartMae === null ? '—' : smartMae.toFixed(2)+'%'}</b></span>
      <span>先停損：基準 <b>${baselineStopFirst}</b> / Smart <b>${smartStopFirst}</b></span>
    </div>
    <div class="callout">這是 Shadow 預期進場，不會送出任何委託。價格路徑依 Bot 約 30～120 秒掃描頻率抽樣，因此不是逐筆成交回放；長期統計主要用來判斷「選股/訊號有問題」還是「等待進場規則有問題」。</div>
  </article>
  <article class="panel smart-table">
    <div class="panel-head">
      <div><h2>Smart Entry 長期追蹤 <span class="count">${rows.length}</span></h2><p>同一筆訊號並排比較 V2.9 原始進場與 SMART_V1</p></div>
      <span>截至 ${esc(date)}</span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>訊號</th><th>股票</th><th>預期區間 / 限價</th><th>V2.9 基準</th><th>SMART_V1</th><th>停損 / Smart 1R / 2R</th><th>Smart MFE / MAE</th><th>Smart 5 / 15 / 30 / 60 分</th></tr></thead>
      <tbody>${rows.length ? rows.map(item => {
        const [label, cls] = smartStatus(item);
        const checkpoint = [item.price_5m,item.price_15m,item.price_30m,item.price_60m].map(v => v === null || v === undefined ? '—' : Number(v).toFixed(2)).join(' / ');
        const baseTags = [item.baseline_hit_1r ? '✓1R' : '', item.baseline_hit_2r ? '✓2R' : '', item.baseline_hit_stop ? '停損' : ''].filter(Boolean).join(' · ') || (item.baseline_done ? '窗口結束' : '追蹤中');
        return `<tr>
          <td>${esc(item.scan_date)}<small>${esc(item.signal_time)} · ${esc(item.model)}</small></td>
          <td><strong>${esc(item.code)}</strong><small>${esc(item.name)}</small></td>
          <td>${Number(item.zone_low).toFixed(2)}～${Number(item.zone_high).toFixed(2)}<small>預期限價 <b>${Number(item.ideal_entry).toFixed(2)}</b></small></td>
          <td>${Number(item.signal_entry).toFixed(2)}<small>${baseTags} · MFE ${item.baseline_mfe_pct === null || item.baseline_mfe_pct === undefined ? '—' : Number(item.baseline_mfe_pct).toFixed(2)+'%'} / MAE ${item.baseline_mae_pct === null || item.baseline_mae_pct === undefined ? '—' : Number(item.baseline_mae_pct).toFixed(2)+'%'}</small></td>
          <td><span class="chip ${cls}">${label}</span><small>${esc(smartOutcome(item))}${item.fill_price !== null && item.fill_price !== undefined ? ' · 成交 '+Number(item.fill_price).toFixed(2) : ''}${item.hit_1r ? ' · ✓1R' : ''}${item.hit_2r ? ' · ✓2R' : ''}</small></td>
          <td>${Number(item.stop).toFixed(2)}<small>1R ${Number(item.target_1r).toFixed(2)} · 2R ${Number(item.target_2r).toFixed(2)}</small></td>
          <td class="${item.mfe_pct ? 'positive' : ''}">${item.mfe_pct === null || item.mfe_pct === undefined ? '—' : '+'+Number(item.mfe_pct).toFixed(2)+'%'}<small class="${item.mae_pct ? 'negative' : ''}">MAE ${item.mae_pct === null || item.mae_pct === undefined ? '—' : Number(item.mae_pct).toFixed(2)+'%'}</small></td>
          <td>${checkpoint}<small>抽樣 ${money(item.samples)} 次</small></td>
        </tr>`;
      }).join('') : empty(8, '目前還沒有 Smart Entry 資料；下一個 V2.9 盤中提醒會自動建立。')}</tbody>
    </table></div>
    <div class="panel-bottom"><span>SMART_V1 固定規則，不回頭修改歷史預期價</span><span>基準與 Smart 都是研究紀錄，不自動下單</span></div>
  </article>`;
}

function tradesView() {
  const rows = selectTrades(snapshot, { ...current(), days, status, query });
  return `<article class="panel"><div class="panel-head"><div><h2>實際交易記錄 <span class="count">${rows.length}</span></h2><p>你自行記錄的真實成交；可編輯與平倉</p></div><div class="table-tools"><input type="search" id="search" placeholder="搜尋代號或名稱" aria-label="搜尋代號或名稱" value="${esc(query)}"><select id="trade-period" aria-label="交易日期範圍">${[1, 7, 30, 90].map(d => `<option value="${d}" ${days === d ? 'selected' : ''}>近 ${d} 日</option>`).join('')}</select><select id="trade-status" aria-label="交易狀態"><option value="all">全部狀態</option><option value="open" ${status === 'open' ? 'selected' : ''}>未平倉</option><option value="closed" ${status === 'closed' ? 'selected' : ''}>已平倉</option></select><button id="export" class="button">↓ 匯出 CSV</button></div></div><div class="table-wrap"><table><thead><tr><th>日期 / 時間</th><th>股票</th><th>進場價</th><th>出場價</th><th>成本</th><th>淨損益 TWD</th><th>狀態</th><th>操作</th></tr></thead><tbody>${rows.length ? rows.map(t => `<tr><td>${esc(t.date)}<small>${esc(t.time)} · 台北</small></td><td><strong>${esc(t.code)}</strong><small>${esc(t.name)}</small></td><td>${Number(t.entry).toFixed(2)}</td><td>${t.status === 'closed' ? Number(t.exit).toFixed(2) : '—'}</td><td>${money(t.costs)}</td><td class="${signClass(t.status === 'closed' ? t.pnl : 0)}">${t.status === 'closed' ? signed(t.pnl) : '—'}</td><td><span class="chip ${t.status === 'closed' ? 'closed' : 'wait'}">${t.status === 'closed' ? '已平倉' : '未平倉'}</span></td><td><button class="table-link" data-detail="${esc(t.id)}">查看</button> · <button class="table-link" data-edit="${esc(t.id)}">編輯</button></td></tr>`).join('') : empty(8, '此篩選條件下沒有實際交易記錄')}</tbody></table></div><div class="panel-bottom"><span>Supabase · 私人 RLS 資料</span><span>共 ${rows.length} 筆</span></div></article>`;
}

function shadowView() {
  const { date, strategy } = current();
  const rows = snapshot.experiments.filter(e => e.date === date && (strategy === 'all' || e.strategy === strategy));
  return `<div class="metrics">${metric('觀察實驗', rows.length || '—', '依選定日期的 Shadow 快照')}${metric('模擬樣本', rows.length ? money(rows.reduce((sum, e) => sum + e.samples, 0)) : '—', '只計 Shadow 模擬樣本')}${metric('執行模式', '獨立', '不混入正式實際績效', 'positive')}${metric('正式策略', 'V2.9', 'Bot 原策略保持不變')}</div><div class="shadow-grid">${rows.length ? rows.map(e => `<article class="panel experiment"><div class="experiment-id">${esc(e.id)} / SHADOW EXPERIMENT</div><span class="chip">${esc(e.status)}</span><h2>${esc(e.name)}</h2><p>${esc(e.description)}</p><div class="experiment-stat"><div><strong>${money(e.samples)}</strong><small>模擬樣本</small></div><div><strong>${percent(e.win_rate)}</strong><small>模擬勝率</small></div><div><strong class="${signClass(e.pnl)}">${signed(e.pnl)}</strong><small>模擬淨損益 TWD</small></div></div></article>`).join('') : '<article class="panel experiment"><div class="experiment-id">SHADOW LAB / READY</div><span class="chip wait">等待資料</span><h2>尚無 Shadow 實驗快照</h2><p>Shadow 會維持獨立，不會把模擬結果算進正式實際成交績效。</p></article>'}</div><div class="panel wide-note"><span>◇</span><div><strong>觀察，不代表正式交易</strong>Shadow 結果只供比較與研究；Dashboard 不會啟停策略或觸發實單。</div></div>`;
}

function render() {
  if ($('#app-root').hidden) return;
  const [title, description] = titles[page];
  $('#crumb').textContent = title;
  $('#page-title').innerHTML = `${title}<span class="title-dot">.</span>`;
  $('#page-description').textContent = description;
  document.title = `${title} · Short Bot`;
  document.querySelectorAll('[data-page]').forEach(a => {
    a.classList.toggle('active', a.dataset.page === page);
    if (a.dataset.page === page) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  renderStatus();
  $('#view').innerHTML = ({ today: todayView, performance: performanceView, smart: smartView, trades: tradesView, shadow: shadowView })[page]();
}

function route() {
  page = Object.hasOwn(titles, location.hash.slice(1)) ? location.hash.slice(1) : 'today';
  render();
}

async function refreshData() {
  if (loading || !session) return;
  loading = true;
  $('#refresh').disabled = true;
  $('#refresh').textContent = '↻ 更新中';
  try {
    const result = await loadDashboard(config, current().date, session);
    session = result.session;
    snapshot = result.snapshot;
    alerts = result.alerts;
    smartEntries = result.smartEntries || [];
    runtime = result.runtime ? { ...result.runtime, candidate_scan_date: result.live?.candidate_scan_date } : null;
    liveError = result.liveError;
    $('#user-email').textContent = session?.user?.email || config.ownerEmail || '';
    render();
  } catch (error) {
    if (error?.status === 401 || /refresh|登入|token|session/i.test(error?.message || '')) {
      session = null;
      showLogin('登入已失效，請重新登入。');
      return;
    }
    liveError = error?.message || '資料讀取失敗';
    render();
  } finally {
    loading = false;
    $('#refresh').disabled = false;
    $('#refresh').textContent = '↻ 更新資料';
  }
}

function openTradeEditor(trade = null) {
  const now = new Date();
  const hhmm = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Taipei', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(now);
  $('#trade-form-title').textContent = trade ? '編輯實際成交' : '新增實際成交';
  $('#trade-id').value = trade?.id || '';
  $('#trade-date').value = trade?.date || current().date;
  $('#trade-time').value = trade?.time || hhmm;
  $('#trade-code').value = trade?.code || '';
  $('#trade-name').value = trade?.name || '';
  $('#trade-entry').value = trade?.entry ?? '';
  $('#trade-quantity').value = trade?.quantity ?? 1000;
  $('#trade-costs').value = trade?.costs ?? 0;
  $('#trade-edit-status').value = trade?.status || 'open';
  $('#trade-exit-date').value = trade?.exit_date || trade?.date || current().date;
  $('#trade-exit-time').value = trade?.exit_time || hhmm;
  $('#trade-exit').value = trade?.exit ?? '';
  $('#trade-note').value = trade?.note || '';
  $('#trade-form-error').textContent = '';
  $('#delete-trade').hidden = !trade;
  toggleCloseFields();
  $('#trade-editor').showModal();
}

function toggleCloseFields() {
  const closed = $('#trade-edit-status').value === 'closed';
  $('#close-fields').hidden = !closed;
  $('#trade-exit').required = closed;
  $('#trade-exit-date').required = closed;
  $('#trade-exit-time').required = closed;
}

function tradePayloadFromForm() {
  return {
    id: $('#trade-id').value || null,
    date: $('#trade-date').value,
    time: $('#trade-time').value,
    code: $('#trade-code').value,
    name: $('#trade-name').value,
    entry: $('#trade-entry').value,
    quantity: $('#trade-quantity').value,
    costs: $('#trade-costs').value,
    status: $('#trade-edit-status').value,
    exit_date: $('#trade-exit-date').value,
    exit_time: $('#trade-exit-time').value,
    exit: $('#trade-exit').value,
    note: $('#trade-note').value,
  };
}

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#login-button');
  button.disabled = true;
  $('#login-error').textContent = '';
  try {
    session = await signIn(config, $('#login-email').value.trim(), $('#login-password').value);
    setAuthVisible(false);
    $('#user-email').textContent = session.user?.email || '';
    await refreshData();
  } catch (error) {
    $('#login-error').textContent = error?.message || '登入失敗。';
  } finally {
    button.disabled = false;
  }
});

$('#logout').addEventListener('click', async () => {
  await signOut(config, session).catch(() => {});
  session = null;
  snapshot = emptySnapshot();
  alerts = [];
  smartEntries = [];
  runtime = null;
  showLogin('');
});

$('#refresh').addEventListener('click', refreshData);
$('#date').addEventListener('change', async () => {
  if (!$('#date').value) $('#date').value = taipeiDate();
  await refreshData();
});
$('#strategy').addEventListener('change', render);
$('#new-trade').addEventListener('click', () => openTradeEditor());
window.addEventListener('hashchange', route);

$('#view').addEventListener('change', event => {
  if (event.target.id === 'trade-period') { days = Number(event.target.value); render(); }
  if (event.target.id === 'trade-status') { status = event.target.value; render(); }
});

$('#view').addEventListener('input', event => {
  if (event.target.id === 'search') {
    query = event.target.value;
    const position = event.target.selectionStart;
    render();
    $('#search')?.focus();
    $('#search')?.setSelectionRange(position, position);
  }
});

$('#view').addEventListener('click', event => {
  const period = event.target.closest('[data-days]');
  if (period) {
    days = Number(period.dataset.days);
    render();
    return;
  }
  if (event.target.closest('#export')) {
    const rows = selectTrades(snapshot, { ...current(), days, status, query });
    const url = URL.createObjectURL(new Blob([csv(rows)], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `short-bot-actual-${current().date}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return;
  }
  const detailButton = event.target.closest('[data-detail]');
  if (detailButton) {
    const trade = snapshot.trades.find(t => t.id === detailButton.dataset.detail);
    if (!trade) return;
    $('#detail').innerHTML = `<span class="chip">ACTUAL · 實際成交</span><h3>${esc(trade.code)} ${esc(trade.name)}</h3><dl class="detail-grid">${[
      ['交易編號', trade.id],
      ['進場時間', `${trade.date} ${trade.time} UTC+8`],
      ['平倉時間', trade.status === 'closed' ? `${trade.exit_date || trade.date} ${trade.exit_time || ''} UTC+8` : '未平倉'],
      ['股數', money(trade.quantity)],
      ['進場價', Number(trade.entry).toFixed(2)],
      ['出場價', trade.status === 'closed' ? Number(trade.exit).toFixed(2) : '—'],
      ['交易成本', money(trade.costs)],
      ['淨損益 TWD', trade.status === 'closed' ? signed(trade.pnl) : '未實現'],
      ['狀態', trade.status === 'closed' ? '已平倉' : '未平倉'],
    ].map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl><p class="detail-note">${esc(trade.note || '尚無附註')}</p>`;
    $('#trade-dialog').showModal();
    return;
  }
  const editButton = event.target.closest('[data-edit]');
  if (editButton) {
    const trade = snapshot.trades.find(t => t.id === editButton.dataset.edit);
    if (trade) openTradeEditor(trade);
  }
});

$('#close-dialog').addEventListener('click', () => $('#trade-dialog').close());
$('#close-editor').addEventListener('click', () => $('#trade-editor').close());
$('#cancel-trade').addEventListener('click', () => $('#trade-editor').close());
$('#trade-edit-status').addEventListener('change', toggleCloseFields);

$('#trade-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#save-trade');
  button.disabled = true;
  $('#trade-form-error').textContent = '';
  try {
    const result = await saveTrade(config, tradePayloadFromForm(), session);
    session = result.session;
    $('#trade-editor').close();
    await refreshData();
  } catch (error) {
    $('#trade-form-error').textContent = error?.message || '儲存失敗。';
  } finally {
    button.disabled = false;
  }
});

$('#delete-trade').addEventListener('click', async () => {
  const id = $('#trade-id').value;
  if (!id || !confirm('確定刪除這筆實際成交紀錄？')) return;
  const button = $('#delete-trade');
  button.disabled = true;
  $('#trade-form-error').textContent = '';
  try {
    session = await deleteTrade(config, id, session);
    $('#trade-editor').close();
    await refreshData();
  } catch (error) {
    $('#trade-form-error').textContent = error?.message || '刪除失敗。';
  } finally {
    button.disabled = false;
  }
});

async function init() {
  route();
  if (!session) {
    showLogin('');
    return;
  }
  setAuthVisible(false);
  $('#user-email').textContent = session?.user?.email || config.ownerEmail || '';
  await refreshData();
}

init();

// Keep the private dashboard in sync while it is open. The loading guard prevents
// overlapping requests; hidden/background tabs do not poll.
setInterval(() => {
  if (session && !document.hidden) refreshData();
}, 60000);
