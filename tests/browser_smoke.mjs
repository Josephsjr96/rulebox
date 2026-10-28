// Optional: Node 22+ and a Chromium CDP instance on localhost:9228.
// Pass the URL of an isolated Rulebox test instance as the first argument.
import assert from 'node:assert/strict';
const app = process.argv[2];
assert(app, 'Pass a test Rulebox URL');
const target = await (await fetch('http://127.0.0.1:9228/json/new?' + encodeURIComponent(app), {method:'PUT'})).json();
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
let id=0; const pending=new Map();
socket.onmessage=event=>{const data=JSON.parse(event.data);if(data.id){const p=pending.get(data.id);pending.delete(data.id);data.error?p.reject(data.error):p.resolve(data.result);}};
const send=(method,params={})=>new Promise((resolve,reject)=>{const next=++id;pending.set(next,{resolve,reject});socket.send(JSON.stringify({id:next,method,params}));});
async function evaluate(expression){const result=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});assert(!result.exceptionDetails,JSON.stringify(result.exceptionDetails));return result.result.value;}
async function until(expression){for(let i=0;i<100;i++){if(await evaluate(expression))return;await new Promise(resolve=>setTimeout(resolve,50));}throw new Error('Timed out: '+expression);}
try {
  await until('!!document.getElementById("project")?.textContent');
  await evaluate('document.getElementById("source").value="Keep explanations concise unless I ask for detail."; document.getElementById("draft").click()');
  await until('!document.getElementById("editor").hidden');
  assert.equal(await evaluate('document.getElementById("instruction").value'),'Keep explanations concise unless I ask for detail.');
  await evaluate('document.getElementById("editor").requestSubmit()');
  await until('document.getElementById("count").textContent === "1"');
  await evaluate('document.getElementById("preview").click()');
  await until('!document.getElementById("preview-panel").hidden');
  assert.match(await evaluate('document.getElementById("after").textContent'),/rulebox:start/);
  await evaluate('document.getElementById("apply").click()');
  await until('document.getElementById("status").textContent.startsWith("Applied to")');
  await send('Page.reload');
  await until('document.getElementById("count")?.textContent === "1"');
  await evaluate('document.querySelector(".rule-actions button:nth-child(2)").click()');
  await until('document.querySelector(".rule-tag").textContent.includes("Disabled")');
  await evaluate('document.getElementById("preview").click()');
  await until('!document.getElementById("preview-panel").hidden');
  assert.doesNotMatch(await evaluate('document.getElementById("after").textContent'),/rulebox:start/);
  await evaluate('document.getElementById("apply").click()');
  await until('document.getElementById("status").textContent.startsWith("Applied to")');
  console.log('PASS: browser draft → edit → save → preview → apply → reload persistence → disable → remove from instructions.');
} finally { socket.close(); await fetch('http://127.0.0.1:9228/json/close/'+target.id); }
