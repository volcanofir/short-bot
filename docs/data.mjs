export const emptySnapshot = () => ({schema_version:1, generated_at:null, candidates:[], trades:[], experiments:[]});
export function taipeiDate(date=new Date()) {return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit'}).format(date);}
export function offsetDate(date, days) {return new Date(Date.parse(date+'T12:00:00+08:00')+days*86400000).toISOString().slice(0,10);}
export function escapeHTML(v) {return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
export function selectTrades(snapshot,{date,days=1,strategy='all',status='all',query=''}={}) {
 const start=offsetDate(date,1-days);
 return snapshot.trades.filter(t=>t.date>=start&&t.date<=date&&(strategy==='all'||t.strategy===strategy)&&(status==='all'||t.status===status)&&`${t.code} ${t.name}`.toLowerCase().includes(query.toLowerCase().trim())).sort((a,b)=>(b.date+b.time).localeCompare(a.date+a.time));
}
// Net P&L is supplied by the producer, including its declared fees/tax/slippage.
export function summarize(trades) {
 const closed=trades.filter(t=>t.status==='closed');
 const pnl=closed.reduce((s,t)=>s+t.pnl,0), wins=closed.filter(t=>t.pnl>0).length;
 const grossWin=closed.reduce((s,t)=>s+Math.max(0,t.pnl),0), grossLoss=-closed.reduce((s,t)=>s+Math.min(0,t.pnl),0);
 let cumulative=0, peak=0, maxDrawdown=0;
 for(const t of [...closed].sort((a,b)=>(a.date+a.time).localeCompare(b.date+b.time))){cumulative+=t.pnl;peak=Math.max(peak,cumulative);maxDrawdown=Math.max(maxDrawdown,peak-cumulative);}
 return {closed:closed.length,pnl,wins,winRate:closed.length?wins/closed.length*100:null,profitFactor:grossLoss?grossWin/grossLoss:grossWin?Infinity:null,maxDrawdown};
}
export function cumulativeSeries(trades,date,days){let sum=0;return Array.from({length:days},(_,i)=>{const day=offsetDate(date,i-days+1);sum+=trades.filter(t=>t.date===day&&t.status==='closed').reduce((s,t)=>s+t.pnl,0);return {date:day,value:sum};});}
export function csv(trades){const fields=['id','date','time','code','name','strategy','status','entry','exit','quantity','pnl'];const cell=v=>'"'+String(v??'').replace(/^[=+@\-\t\r]/,"'$&").replace(/"/g,'""')+'"';return '\uFEFF'+[fields,...trades.map(t=>fields.map(f=>t[f]))].map(r=>r.map(cell).join(',')).join('\r\n');}
const finite=v=>typeof v==='number'&&Number.isFinite(v);
const validDate=v=>typeof v==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(v)&&!Number.isNaN(Date.parse(v))&&new Date(v).toISOString().slice(0,10)===v;
const text=v=>typeof v==='string'&&v.length<=1000;
export function validateSnapshot(p){
 const fail=()=>{throw new Error('資料格式不符 dashboard snapshot v1，請檢查資料介面。');};
 if(!p||p.schema_version!==1||!text(p.generated_at)||Number.isNaN(Date.parse(p.generated_at)))fail();
 for(const key of ['candidates','trades','experiments'])if(!Array.isArray(p[key])||p[key].length>10000)fail();
 const ids=new Set();
 for(const t of p.trades){if(!['id','date','time','code','name','strategy'].every(k=>text(t[k]))||ids.has(t.id)||!validDate(t.date)||!/^\d{2}:\d{2}$/.test(t.time)||!['open','closed'].includes(t.status)||!finite(t.entry)||!finite(t.quantity)||(t.status==='closed'&&(!finite(t.pnl)||!finite(t.exit)))||!text(t.note??''))fail();ids.add(t.id);}
 for(const c of p.candidates)if(!['code','name','strategy','setup','chip','status'].every(k=>text(c[k]))||!validDate(c.date)||!finite(c.price)||!finite(c.change)||!finite(c.score))fail();
 for(const e of p.experiments)if(!['id','name','description','strategy','status'].every(k=>text(e[k]))||!validDate(e.date)||!finite(e.samples)||!finite(e.pnl)||(e.win_rate!==null&&(!finite(e.win_rate)||e.win_rate<0||e.win_rate>100)))fail();
 return p;
}
export async function loadSnapshot(config,fetcher=fetch){
 const {supabaseUrl:url,supabasePublishableKey:key}=config;
 if(!url&&!key)return {source:'empty',snapshot:emptySnapshot()};
 if(!url||!key)throw new Error('Supabase 設定不完整：需要專案網址與 publishable key。');
 if(!/^sb_publishable_[A-Za-z0-9_-]+$/.test(key))throw new Error('前台僅接受 sb_publishable_ 金鑰。禁止使用 secret 或 service_role。');
 const base=new URL(url);if(base.protocol!=='https:'||base.username||base.password||base.search||base.hash||base.pathname!=='/')throw new Error('Supabase 網址必須是 HTTPS 專案根網址。');
 const response=await fetcher(new URL('/rest/v1/dashboard_snapshots?select=payload&order=created_at.desc&limit=1',base),{headers:{apikey:key,Accept:'application/json'},cache:'no-store',signal:AbortSignal.timeout(12000)});
 if(!response.ok)throw new Error(`資料讀取失敗（HTTP ${response.status}），請檢查唯讀權限與連線。`);
 const rows=await response.json();if(!Array.isArray(rows))throw new Error('資料回應格式不正確。');
 return {source:'supabase',snapshot:rows.length?validateSnapshot(rows[0].payload):emptySnapshot()};
}
export function demoSnapshot(today=taipeiDate()){
 const names=[['2330','台積電'],['2317','鴻海'],['2454','聯發科'],['3231','緯創'],['2382','廣達'],['2603','長榮']];
 const candidates=names.map(([code,name],i)=>({code,name,date:today,strategy:'V2.9',price:[1040,185.5,1380,112,278,215.5][i],change:[-1.4,-2.1,.8,-1.8,-.6,1.2][i],score:[8,7,6,8,5,4][i],setup:['反彈轉弱','跌破觀察線','等待確認'][i%3],chip:['集中','分散雜亂','普通'][i%3],status:i%3===2?'觀察中':'訊號確認'}));
 const trades=Array.from({length:48},(_,i)=>{const [code,name]=names[i%6],entry=[1040,185.5,1380,112,278,215.5][i%6],pnl=[1850,-1200,2400,900,-800,1600,0,2150][i%8];return {id:`DEMO-${i+1}`,date:offsetDate(today,-Math.floor(i/2)),time:i%2?'10:15':'09:25',code,name,strategy:'V2.9',status:i===0?'open':'closed',entry,exit:i===0?null:Number((entry-pnl/1000).toFixed(2)),quantity:1000,pnl:i===0?null:pnl,note:'此筆為合成示範交易，僅用來測試前台功能，並非 V2.9 真實績效。示範淨損益已包含模擬交易成本。'};});
 const experiments=[{id:'SH-001',name:'二日籌碼延續度',description:'比較主要買方重疊率，觀察籌碼延續對訊號的影響。',samples:86,win_rate:61.6,pnl:12400},{id:'SH-002',name:'分散雜亂加分',description:'比較有效空方型態成立後，加分條件的模擬表現。',samples:64,win_rate:57.8,pnl:8100},{id:'SH-003',name:'次波反彈再進場',description:'觀察 10:00 後反彈轉弱訊號，與主策略獨立記錄。',samples:32,win_rate:53.1,pnl:-1800}].map(e=>({...e,date:today,strategy:'V2.9',status:'模擬觀察'}));
 return {schema_version:1,generated_at:new Date().toISOString(),candidates,trades,experiments};
}
