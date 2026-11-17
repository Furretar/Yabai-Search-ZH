import { spawn } from "node:child_process";
// Which dictionaries a result row shows, for EVERY combination of selected dictionaries.
//
// Loads the real page in headless Chromium and runs the page's own initialDictSelection() on synthetic rows:
// every set of selected dictionaries x every non-empty set of dictionaries that define the word (2^N x 2^N,
// about 65 thousand cases for 8 dictionaries). The rules, none of which names a dictionary or language:
//   1. a row always shows something, and only dictionaries that define it;
//   2. if a selected dictionary defines it, only selected ones show (the main section);
//   3. if none does ("Other Dictionaries"), every language that has a definition is represented.
// Needs a running site (YABAI_TEST_URL, default the dev service) and chromium; skips with exit 0 when either is
// missing, so a machine without a browser can still run the rest of the tests.
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const port = 9344;
const url = process.env.YABAI_TEST_URL || "http://127.0.0.1:8001/";
const profile = mkdtempSync(join(tmpdir(), "yabai-combos-"));
try { const r = await fetch(url, { signal: AbortSignal.timeout(5000) }); if (!r.ok) throw new Error(r.status); } catch (e) { console.log(`skipped: ${url} is not reachable (${e.message})`); process.exit(0); }
const chrome = spawn(process.env.CHROMIUM || "chromium", ["--headless=new","--no-sandbox","--disable-gpu",`--remote-debugging-port=${port}`,"--user-data-dir="+profile,"about:blank"],{stdio:"ignore"});
const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
let tabs; for(let i=0;i<50;i++){ try{ tabs=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); if(tabs.length) break;}catch{} await sleep(200);}
const ws=new WebSocket(tabs.find(t=>t.type==="page").webSocketDebuggerUrl);
await new Promise(r=>ws.onopen=r);
let id=0; const pend=new Map();
ws.onmessage=(m)=>{const d=JSON.parse(m.data); if(d.id&&pend.has(d.id)){pend.get(d.id)(d);pend.delete(d.id);}};
const send=(method,params={})=>new Promise(res=>{const i=++id;pend.set(i,res);ws.send(JSON.stringify({id:i,method,params}));});
await send("Page.enable"); await send("Page.navigate",{url}); await sleep(6000);
const expr = `(() => {
  const anchors = dictToggles.map(t => t.dataset.anchor);
  const n = anchors.length; let bad = [], checked = 0, badCount = 0;
  state.customDicts = false; state.compact = true;
  for (let sm = 0; sm < (1<<n); sm++) {
    const S = expandMergedSelection(new Set(anchors.filter((a,i)=>sm>>i&1)));
    state.dicts = S;
    for (let dm = 1; dm < (1<<n); dm++) {
      const D = anchors.filter((a,i)=>dm>>i&1);
      const entry = { simplified:"x", traditional:"x", definitions: D.map(a => ({anchor:a, lang:anchorLang.get(a), defs:["d"], groups:[]})), forms: [] };
      entryDictState.clear();
      const got = initialDictSelection(entry);
      checked++;
      const inter = D.filter(a=>S.has(a));
      const fail = (why)=>{ badCount++; if (bad.length<8) bad.push({why, S:[...S], D, got:[...got]}); };
      if (!got.size) { fail("nothing shown"); continue; }
      if ([...got].some(a=>!D.includes(a))) { fail("shows a dictionary that does not define it"); continue; }
      if (inter.length) { if ([...got].some(a=>!S.has(a))) fail("selected row shows an unselected dictionary"); }
      else {
        const langs = new Set(D.map(a=>anchorLang.get(a)));
        const gotLangs = new Set([...got].map(a=>anchorLang.get(a)));
        if ([...langs].some(l=>!gotLangs.has(l))) fail("other section hides a language");
      }
    }
  }
  return JSON.stringify({anchors, checked, badCount, bad});
})()`;
const r=await send("Runtime.evaluate",{expression:expr,returnByValue:true});
const out = JSON.parse(r.result?.result?.value ?? "null");
ws.close();
const exited = new Promise((resolve) => chrome.once("exit", resolve));
chrome.kill();
await Promise.race([exited, sleep(5000)]);
try { rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch {}
if (!out) { console.log("no result from the page:", JSON.stringify(r)); process.exit(1); }
console.log(`${out.checked} combinations of ${out.anchors.length} dictionaries checked, ${out.badCount} wrong`);
if (out.badCount) { console.log(JSON.stringify(out.bad, null, 1)); process.exit(1); }
