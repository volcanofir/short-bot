import test from 'node:test';
import assert from 'node:assert/strict';
import {taipeiDate,offsetDate,selectTrades,summarize,csv,validateSnapshot,loadSnapshot,escapeHTML,cumulativeSeries} from '../docs/data.mjs';

function fixtureSnapshot(today='2026-09-23'){
  const trades=[
    {id:'T1',date:today,time:'09:25',code:'2330',name:'台積電',strategy:'V2.9',status:'open',entry:1040,exit:null,quantity:1000,pnl:null,note:''},
    {id:'T2',date:today,time:'10:15',code:'2317',name:'鴻海',strategy:'V2.9',status:'closed',entry:185.5,exit:184.5,quantity:1000,pnl:1000,note:''},
  ];
  for(let i=1;i<7;i++){
    const day=offsetDate(today,-i);
    trades.push(
      {id:`A${i}`,date:day,time:'09:25',code:'2330',name:'台積電',strategy:'V2.9',status:'closed',entry:100,exit:99,quantity:1000,pnl:1000,note:''},
      {id:`B${i}`,date:day,time:'10:15',code:'2317',name:'鴻海',strategy:'V2.9',status:'closed',entry:100,exit:101,quantity:1000,pnl:-1000,note:''},
    );
  }
  return {
    schema_version:1,
    generated_at:new Date().toISOString(),
    candidates:[{code:'2330',name:'台積電',date:today,strategy:'V2.9',price:1040,change:-1.4,score:8,setup:'反彈轉弱',chip:'集中',status:'訊號確認'}],
    trades,
    experiments:[{id:'SH-001',date:today,strategy:'V2.9',name:'測試',description:'fixture',status:'模擬觀察',samples:10,win_rate:50,pnl:0}],
  };
}
test('Taipei date crosses UTC day and month boundaries',()=>{assert.equal(taipeiDate(new Date('2026-09-22T17:00:00Z')),'2026-09-23');assert.equal(offsetDate('2026-03-01',-1),'2026-02-28');});
test('demo satisfies snapshot contract; malformed and duplicate data rejected',()=>{const d=fixtureSnapshot('2026-09-23');assert.equal(validateSnapshot(d),d);assert.throws(()=>validateSnapshot({...d,schema_version:2}));assert.throws(()=>validateSnapshot({...d,trades:[d.trades[0],d.trades[0]]}));assert.throws(()=>validateSnapshot({...d,trades:[{...d.trades[1],pnl:null}]}));});
test('filters combine date, strategy, status and query',()=>{const d=fixtureSnapshot('2026-09-23');assert.equal(selectTrades(d,{date:'2026-09-23'}).length,2);assert.equal(selectTrades(d,{date:'2026-09-23',days:7}).length,14);assert.equal(selectTrades(d,{date:'2026-09-23',days:7,status:'open',query:'台積'}).length,1);assert.equal(selectTrades(d,{date:'2026-09-23',strategy:'nonexistent'}).length,0);assert.equal(selectTrades(d,{date:'2020-01-01'}).length,0);});
test('performance ignores open trades and counts flat trades in denominator',()=>{const d=fixtureSnapshot('2026-09-23');const rows=[100,-60,0,40].map((pnl,i)=>({...d.trades[1],pnl,date:`2026-09-0${i+1}`}));const s=summarize([...rows,{...d.trades[0],pnl:99999}]);assert.equal(s.pnl,80);assert.equal(s.winRate,50);assert.equal(s.profitFactor,140/60);assert.equal(s.maxDrawdown,60);assert.equal(summarize([]).winRate,null);assert.equal(cumulativeSeries(rows,'2026-09-04',4).at(-1).value,80);});
test('CSV neutralizes spreadsheet formulas and escapes quotes; HTML escaped',()=>{const result=csv([{id:'=CMD()',name:'a"b',pnl:-30}]);assert.ok(result.includes("'=CMD()"));assert.ok(result.includes('a""b'));assert.equal(escapeHTML('<img onerror="x">'),'&lt;img onerror=&quot;x&quot;&gt;');});
test('unconfigured adapter makes no requests; partial and secret configs rejected',async()=>{let called=false;assert.equal((await loadSnapshot({},()=>{called=true})).source,'empty');assert.equal(called,false);await assert.rejects(()=>loadSnapshot({supabaseUrl:'https://example.supabase.co'}));await assert.rejects(()=>loadSnapshot({supabaseUrl:'https://example.supabase.co',supabasePublishableKey:'sb_secret_bad'}));});
test('adapter is GET only; publishable key in apikey; validates and surfaces errors',async()=>{const config={supabaseUrl:'https://example.supabase.co',supabasePublishableKey:'sb_publishable_test'};const d=fixtureSnapshot();const result=await loadSnapshot(config,async(url,options)=>{assert.equal(url.pathname,'/rest/v1/dashboard_snapshots');assert.equal(options.method,undefined);assert.equal(options.headers.apikey,config.supabasePublishableKey);assert.equal(options.headers.Authorization,undefined);return {ok:true,json:async()=>[{payload:d}]};});assert.equal(result.snapshot,d);await assert.rejects(()=>loadSnapshot(config,async()=>({ok:false,status:403})),/403/);await assert.rejects(()=>loadSnapshot(config,async()=>({ok:true,json:async()=>[{payload:{}}]})));});
