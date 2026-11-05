// Synthetic history/API records only; none are actual exchange observations.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import ts from "typescript";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import TestRenderer, { act } from "react-test-renderer";

const require = createRequire(import.meta.url), cache = new Map();
let fetchHistory = async () => { throw new Error("Unexpected test request"); };
function load(url) {
  if (cache.has(url.href)) return cache.get(url.href);
  const source = fs.readFileSync(url,"utf8");
  const output = ts.transpileModule(source,{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  const module = {exports:{}};
  cache.set(url.href,module.exports);
  const localRequire = name => {
    if (name.endsWith(".css")) return {};
    if (name.includes("portfolio-api")) return {request:(...args)=>fetchHistory(...args)};
    if (name.startsWith(".")) for (const extension of [".tsx",".ts"]) {
      const local = new URL(name+extension,url);
      if(fs.existsSync(local)) return load(local);
    }
    return require(name);
  };
  new Function("require","module","exports",output)(localRequire,module,module.exports);
  return module.exports;
}
const helpers=load(new URL("../app/lib/etf-price-history.ts",import.meta.url));
const {EtfPriceHistory,EtfHistoryPlot}=load(new URL("../app/components/etf-price-history.tsx",import.meta.url));
const ID="00000000-0000-0000-0000-000000000071";
function bars(count,start="2025-01-01") {
  const result=[],day=new Date(start);
  while(result.length<count) {
    if(day.getUTCDay()!==0&&day.getUTCDay()!==6) result.push({date:day.toISOString().slice(0,10),close:String(100+result.length),open:null,high:null,low:null,volume:result.length===0?"0":null,gap_before:false,provider:"NSE",provider_symbol:"SYNTHETF",currency:"INR",source_url:"https://nsearchives.nseindia.com/SYNTHETIC_TEST_ONLY.csv",retrieved_at:"2026-09-15T12:00:00+00:00"});
    day.setUTCDate(day.getUTCDate()+1);
  }
  return result;
}
function page(overrides={}) {
  const observations=bars(100);
  return {instrument_id:ID,symbol:"SYNTHETF",isin:"IN0000000071",provider:"NSE",currency:"INR",exchange:"NSE",mic:"XNSE",price_basis:"UNADJUSTED_CLOSE",range:{name:"MAX",start:null,end:"2026-09-15"},available:{first_date:observations[0].date,last_date:observations.at(-1).date,stored_rows:observations.length,observations:null,validation_scope:"EACH_RETURNED_PAGE"},available_providers:[{provider:"NSE",first_date:observations[0].date,last_date:observations.at(-1).date,stored_rows:observations.length}],bars:observations,warmup:[],next_cursor:null,as_of:"2026-09-15T12:00:00+00:00",diagnostics:{warnings:["UNADJUSTED_PRICE_NOT_TOTAL_RETURN"],rejected_rows:0,calendar_state:"PERSISTED_SESSION_SCHEDULE",missing_sessions:[]},benchmark:{status:"UNAVAILABLE",reason:"NO_VERIFIED_COMPARABLE_BENCHMARK_SERIES"},...overrides};
}

test("history parser preserves decimal strings, absent volume, and real zero",()=>{
  const data=helpers.parseHistoryPage(page(),ID);
  assert.equal(data.bars[0].volume,"0"); assert.equal(data.bars[1].volume,null);
  assert.equal(data.bars[0].close,"100"); assert.equal(data.price_basis,"UNADJUSTED_CLOSE");
});
for(const [name,change] of [
  ["wrong instrument",p=>p.instrument_id="another"], ["mixed venue",p=>p.exchange="BSE"],
  ["unknown provider",p=>p.provider="UNVERIFIED"], ["adjusted basis",p=>p.price_basis="ADJUSTED"],
  ["calendar unavailable",p=>p.diagnostics.calendar_state="UNKNOWN"],
  ["wrong currency",p=>p.bars[0].currency="USD"], ["provider mismatch",p=>p.bars[0].provider="YAHOO_FINANCE"],
  ["wrong symbol",p=>p.bars[0].provider_symbol="OTHER"], ["missing date",p=>delete p.bars[0].date],
  ["invalid date",p=>p.bars[0].date="2025-02-30"], ["duplicate date",p=>p.bars[1].date=p.bars[0].date],
  ["missing close",p=>p.bars[0].close=null], ["zero close",p=>p.bars[0].close="0"],
  ["nonfinite close",p=>p.bars[0].close="NaN"], ["hexadecimal close",p=>p.bars[0].close="0x64"],
  ["negative volume",p=>p.bars[0].volume="-1"], ["inexact volume JSON number",p=>p.bars[0].volume=1],
  ["volume overflow",p=>p.bars[0].volume="99999999999999999999999999999"],
  ["future retrieval",p=>p.bars[0].retrieved_at="2027-01-01T00:00:00Z"],
  ["naive retrieval",p=>p.bars[0].retrieved_at="2025-01-01T12:00:00"],
  ["unapproved source",p=>p.bars[0].source_url="https://nseindia.com.evil.test/record"],
  ["missing gap state",p=>delete p.bars[0].gap_before],
  ["invalid OHLC",p=>{p.bars[0].high="10";p.bars[0].low="20";}],
  ["invented benchmark",p=>p.benchmark={status:"AVAILABLE",reason:""}],
]) test(`invalid history fails visibly: ${name}`,()=>{
  const value=page(); change(value);
  assert.throws(()=>helpers.parseHistoryPage(value,ID),/could not be validated/);
});

test("SMA50/150/200 require full observation counts, never partial averages",()=>{
  const result=helpers.historyIndicators(bars(201));
  assert.equal(result[48].sma50,null);assert.equal(result[49].sma50,124.5);
  assert.equal(result[148].sma150,null);assert.equal(result[149].sma150,174.5);
  assert.equal(result[198].sma200,null);assert.equal(result[199].sma200,199.5);
});
test("missing configured sessions reset SMA windows without inserting prices",()=>{
  const source=bars(201);source[100].gap_before=true;
  const result=helpers.historyIndicators(source);
  assert.equal(result.length,201);assert.equal(result[100].sma50,null);
  assert.equal(result[148].sma50,null);assert.notEqual(result[149].sma50,null);
  assert.equal(result.at(-1).sma150,null);assert.equal(result.at(-1).sma200,null);
});
test("verified warmup supplies a full first visible moving average",()=>{
  const source=bars(210),result=helpers.historyIndicators(source.slice(199),source.slice(0,199));
  assert.equal(result.length,11);assert.equal(result[0].sma200,199.5);
});
test("peaks and drawdown use observed closes within the view",()=>{
  const source=bars(4).map((b,i)=>({...b,close:String([100,120,90,110][i])}));
  const result=helpers.historyExtremes(helpers.historyIndicators(source));
  assert.equal(result.peak.date,source[1].date);assert.equal(result.maxDrawdown,-25);
  assert.ok(Math.abs(result.currentDrawdown+8.33333333333333)<1e-9);
  assert.equal(helpers.historyExtremes([]).peak,null);
});
test("zoom and pan clamp to actual observations",()=>{
  assert.deepEqual(helpers.historyWindow(100,-10,20),{start:0,size:20});
  assert.deepEqual(helpers.historyWindow(100,95,20),{start:80,size:20});
  assert.deepEqual(helpers.historyWindow(100,0,1000),{start:0,size:100});
  assert.deepEqual(helpers.historyWindow(0,0,0),{start:0,size:0});
});

test("pagination binds source and range and never fetches a universe",async()=>{
  const data=bars(4),calls=[];
  const source=async path=>{
    calls.push(path);
    return calls.length===1?page({bars:data.slice(0,2),next_cursor:data[1].date}):page({bars:data.slice(2)});
  };
  const result=await helpers.loadEtfHistory(ID,{range:"MAX"},source);
  assert.equal(result.bars.length,4);assert.equal(result.pages,2);assert.equal(result.truncated,false);
  assert.match(calls[0],new RegExp(`/instruments/${ID}/history\\?`));
  assert.match(calls[1],/provider=NSE/);assert.match(calls[1],/after=2025-01-02/);
  assert.equal(calls.some(p=>p.includes("full-universe")),false);
});
test("bounded pages expose truncated coverage instead of unbounded fetch",async()=>{
  const data=bars(3);let calls=0;
  const result=await helpers.loadEtfHistory(ID,{range:"MAX"},async()=>page({bars:[data[calls]],next_cursor:data[calls++].date}),undefined,2);
  assert.equal(calls,2);assert.equal(result.truncated,true);assert.equal(result.bars.length,2);
});
test("repeated cursor and provider changes fail instead of looping or mixing",async()=>{
  const data=bars(4);let calls=0;
  await assert.rejects(helpers.loadEtfHistory(ID,{range:"MAX"},async()=>page({bars:[data[calls++]],next_cursor:data[0].date})),/validated/);
  calls=0;
  await assert.rejects(helpers.loadEtfHistory(ID,{range:"MAX"},async()=>{
    calls++;return calls===1?page({bars:data.slice(0,2),next_cursor:data[1].date}):page({bars:[],provider:"YAHOO_FINANCE"});
  }),/validated/);
});
test("abort cancels read before another page",async()=>{
  const controller=new AbortController();controller.abort();let calls=0;
  await assert.rejects(helpers.loadEtfHistory(ID,{range:"MAX"},async()=>{calls++;return page();},controller.signal),/cancelled/);
  assert.equal(calls,0);
});
test("gap segments and responsive plot expose accessible source labels",()=>{
  const data=bars(3);data[2].gap_before=true;
  const html=renderToStaticMarkup(React.createElement(EtfHistoryPlot,{points:helpers.historyIndicators(data),width:360}));
  assert.match(html,/role="img"/);assert.match(html,/viewBox="0 0 360 334"/);
  assert.match(html,/Unadjusted ETF close/);assert.match(html,/Volume/);
  const path=html.match(/<path d="([^"]*)" class="etf-history-line"/)[1];
  assert.equal((path.match(/M/g)||[]).length,2);
});

const flush=()=>new Promise(resolve=>setImmediate(resolve));
function respond(path) {
  const params=new URL(path,"http://synthetic.test").searchParams;
  return page({bars:bars(100,"2026-01-01"),range:{name:params.get("range"),start:params.get("start")??"2025-09-15",end:params.get("end")??"2026-09-15"}});
}
test("range controls call authenticated request wrapper and indicators/zoom are interactive",async()=>{
  const calls=[];fetchHistory=async path=>{calls.push(path);return respond(path);};let root;
  try {
    await act(async()=>{root=TestRenderer.create(React.createElement(EtfPriceHistory,{instrumentId:ID,symbol:"SYNTHETF"}));await flush();});
    const button=root.root.findAllByType("button").find(b=>b.children.join("")==="3Y");
    await act(async()=>{button.props.onClick();await flush();});
    assert.match(calls.at(-1),/range=3Y/);
    const check=root.root.findAllByType("input").find(i=>i.props.type==="checkbox");
    act(()=>check.props.onChange());
    assert.equal(check.props.checked,false);
    const zoom=root.root.findAllByType("button").find(b=>b.props["aria-label"]==="Zoom into price history");
    assert.equal(zoom.props.disabled,false);act(()=>zoom.props.onClick());
    const later=root.root.findAllByType("button").find(b=>b.props["aria-label"]==="Pan history later");
    assert.equal(later.props.disabled,false);
    assert.match(JSON.stringify(root.toJSON()),/not total returns/);
  } finally {if(root) act(()=>root.unmount());}
});
test("API failure remains visible and retry performs a fresh bounded read",async()=>{
  let calls=0;fetchHistory=async path=>{if(++calls===1)throw new Error("Synthetic persisted source unavailable");return respond(path);};let root;
  try {
    await act(async()=>{root=TestRenderer.create(React.createElement(EtfPriceHistory,{instrumentId:ID}));await flush();});
    assert.match(JSON.stringify(root.toJSON()),/Synthetic persisted source unavailable/);
    const retry=root.root.findAllByType("button").find(b=>b.children.join("")==="Retry price history");
    await act(async()=>{retry.props.onClick();await flush();});
    assert.equal(calls,2);assert.equal(root.root.findAllByProps({role:"alert"}).length,0);
  } finally {if(root) act(()=>root.unmount());}
});
