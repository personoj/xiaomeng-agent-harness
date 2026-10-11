import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import { VoiceClient } from '../web/client.js';

test('microphone worklet emits 20ms PCM16 frames at 16 kHz from 48 kHz', () => {
  let Processor; const messages=[];
  const context=vm.createContext({sampleRate:48000,Int16Array,AudioWorkletProcessor:class{constructor(){this.port={postMessage:b=>messages.push(new Int16Array(b))};}},registerProcessor:(name,cls)=>{Processor=cls;}});
  vm.runInContext(fs.readFileSync(new URL('../web/audio-worklet.js',import.meta.url),'utf8'),context);
  const capture=new Processor();
  for(let i=0;i<15;i++)capture.process([[new Float32Array(128).fill(.5)]]);
  assert.equal(messages.length,2);assert.equal(messages[0].length,320);
  assert.ok(messages[0].every(sample=>sample===16383));
});

function runtime() {
  const sent=[];const nodes=[];
  const c=new VoiceClient({baseURL:'http://localhost:8766'});
  c.ws={readyState:1,send:event=>sent.push(JSON.parse(event)),close(){}};
  c.audio={state:'running',currentTime:0,destination:{},close(){},createBuffer:(_,length)=>({duration:length/24000,getChannelData:()=>new Float32Array(length)}),createBufferSource:()=>{const s={connect(){},start(at){this.at=at;},stop(){this.stopped=true;}};nodes.push(s);return s;}};
  return {c,sent,nodes};
}

const chunk=(id='r1',seq=1)=>({type:'audio.delta',response_id:id,seq,audio:Buffer.alloc(4800).toString('base64')});

test('generated audio is not reported drained before actual playback ends',async()=>{
  const {c,sent,nodes}=runtime();await c.receive(chunk());await c.receive({type:'audio.end',response_id:'r1'});
  assert.equal(sent.at(-1).drained,false);
  c.audio.currentTime=.125;nodes[0].onended();
  assert.equal(sent.at(-1).drained,true);assert.equal(sent.at(-1).played_samples,2400);
});

test('interrupt reports actually played samples and blocks late frames',async()=>{
  const {c,sent,nodes}=runtime();await c.receive(chunk());c.audio.currentTime=.075;
  await c.receive({type:'audio.clear',response_ids:['r1']});
  assert.ok(sent.at(-1).played_samples>=1199&&sent.at(-1).played_samples<=1200);
  assert.equal(sent.at(-1).drained,false);assert.equal(nodes[0].stopped,true);
  await c.receive(chunk('r1',2));assert.equal(nodes.length,1);
});

test('interrupt after all samples play cannot drain a delayed audio.end',async()=>{
  const {c,sent,nodes}=runtime();await c.receive(chunk());
  c.audio.currentTime=1;nodes[0].onended();
  const progress={type:'playback.progress',response_id:'r1',played_samples:2400,drained:false};
  assert.deepEqual(sent,[progress]);
  // Generation can be done at Harness while its end marker is still in transit.
  c.interrupt();
  assert.deepEqual(sent,[progress,progress,{type:'interrupt'}]);
  assert.equal(c.playing.has('r1'),false);
  await c.receive({type:'audio.end',response_id:'r1'});
  await c.receive(chunk('r1',2));c.reportProgress();
  assert.equal(sent.length,3);assert.equal(nodes.length,1);
  assert.equal(sent.some(event=>event.drained),false);
});

test('duplicate audio sequence is not played twice',async()=>{
  const {c,nodes}=runtime();await c.receive(chunk());await c.receive(chunk());assert.equal(nodes.length,1);
});

test('page duplicate tool request executes once and returns cached acknowledgement',async()=>{
  const {c,sent}=runtime();let effects=0;c.revision=3;
  c.registerActions([{name:'change',description:'test',parameters:{type:'object'},execute:async()=>{effects++;return{result:{ok:true},context:{region:'华东'}};}}]);
  const request={type:'tool.request',call_id:'c1',name:'change',args:{},expected_revision:3};
  await c.receive(request);await c.actionChain;await c.receive(request);await c.actionChain;
  assert.equal(effects,1);assert.equal(sent.filter(x=>x.type==='tool.result').length,2);assert.equal(c.revision,4);
});

test('page action with stale revision is rejected before effect',async()=>{
  const {c,sent}=runtime();let effects=0;c.revision=4;
  c.registerActions([{name:'change',parameters:{type:'object'},execute:async()=>{effects++;return{};}}]);
  await c.receive({type:'tool.request',call_id:'c1',name:'change',args:{},expected_revision:3});await c.actionChain;
  assert.equal(effects,0);assert.equal(sent.at(-1).result.error,'page_context_changed');
});

function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
const request=(callId='c1',turn=1)=>({type:'tool.request',call_id:callId,name:'change',args:{},expected_revision:3,turn});

test('BOOT turn invalidates an in-flight intent without changing page revision',async()=>{
  const {c,sent}=runtime();const read=deferred(),started=deferred();let effects=0;c.revision=3;
  c.registerActions([{name:'change',parameters:{type:'object'},execute:async(_,execution)=>{
    started.resolve();await read.promise;
    if(!execution.isCurrent())return {result:{error:'turn_superseded'}};
    effects++;return {result:{ok:true}};
  }}]);
  await c.receive(request());await started.promise;
  await c.receive({type:'turn.started',turn:2,reason:'manual_audio'});
  // A previous dispatched operation may still finish. It must not revive turn 1.
  await c.receive({type:'tool.completed',turn:1,call_id:'older'});
  read.resolve();await c.actionChain;
  assert.equal(c.revision,3);assert.equal(c.turn,2);assert.equal(effects,0);
  assert.equal(sent.at(-1).result.error,'turn_superseded');
});

test('queued old-turn tool is rejected before its action starts',async()=>{
  const {c,sent}=runtime();const hold=deferred(),started=deferred();let calls=0;c.revision=3;
  c.registerActions([{name:'change',parameters:{type:'object'},execute:async()=>{
    calls++;started.resolve();await hold.promise;return {result:{ok:true},context:{stale:true}};
  }}]);
  await c.receive(request('first'));await started.promise;await c.receive(request('queued'));
  await c.receive({type:'turn.started',turn:2,reason:'manual_audio'});
  hold.resolve();await c.actionChain;
  assert.equal(calls,1);assert.equal(sent.at(-1).result.error,'turn_superseded');
  assert.equal(c.revision,3);assert.deepEqual(c.context,{});
  // The first action was already submitted; its real successful outcome is retained.
  assert.equal(sent.find(e=>e.call_id==='first').result.ok,true);
});

test('transport replacement invalidates old closures and never sends old reply on new socket',async()=>{
  const {c,sent}=runtime();const read=deferred(),started=deferred();let current,executions=0;c.revision=3;
  c.registerActions([{name:'change',parameters:{type:'object'},execute:async(_,execution)=>{
    executions++;started.resolve();await read.promise;current=execution.isCurrent();
    return {result:{accepted:true,task_id:'already-running'},context:{stale:true}};
  }}]);
  await c.receive(request());await started.promise;
  c.disconnect();const replacement=[];
  c.ws={readyState:1,send:event=>replacement.push(JSON.parse(event)),close(){}};
  read.resolve();await c.actionChain;
  assert.equal(current,false);assert.equal(replacement.length,0);
  assert.deepEqual(c.context,{});assert.equal(sent.filter(e=>e.type==='tool.result').length,0);
  // Returning the known result on an explicit duplicate preserves idempotency.
  await c.receive(request());await c.actionChain;
  assert.equal(executions,1);assert.equal(replacement.at(-1).result.task_id,'already-running');
});

test('actual socket close invalidates the transport generation',async()=>{
  const originalFetch=globalThis.fetch,originalWebSocket=globalThis.WebSocket;
  const sockets=[];
  class FakeSocket{
    static OPEN=1;
    constructor(){this.readyState=1;sockets.push(this);queueMicrotask(()=>this.onmessage({data:JSON.stringify({type:'attached'})}));}
    send(){}
    close(){this.readyState=3;this.onclose?.();}
  }
  globalThis.fetch=async()=>({ok:true,json:async()=>({session_id:'synthetic'})});
  globalThis.WebSocket=FakeSocket;
  const c=new VoiceClient({baseURL:'http://localhost:8766'});
  try{
    await c.connect();c.revision=3;let execution;
    c.registerActions([{name:'change',parameters:{type:'object'},execute:async(_,value)=>{execution=value;return {};}}]);
    await c.receive(request());await c.actionChain;assert.equal(execution.isCurrent(),true);
    sockets[0].close();assert.equal(execution.isCurrent(),false);
    await c.connect();assert.equal(execution.isCurrent(),false);
    sockets[0].onmessage({data:JSON.stringify({type:'turn.started',turn:99})});
    assert.equal(c.turn,null);
  }finally{
    c.disconnect();globalThis.fetch=originalFetch;globalThis.WebSocket=originalWebSocket;
  }
});

for(const transition of [
  {type:'state',state:'disconnected',turn:1},
  {type:'state',state:'error',turn:1},
  {type:'audio.clear',response_ids:['source-response'],turn:1},
])test(`${transition.type} ${transition.state||'source response'} invalidates pending intent on the same socket`,async()=>{
  const {c,sent}=runtime();const read=deferred(),started=deferred();let effects=0;c.revision=3;
  c.registerActions([{name:'change',parameters:{type:'object'},execute:async(_,execution)=>{
    started.resolve();await read.promise;
    if(!execution.isCurrent())return {result:{error:'cancelled'}};
    effects++;return {result:{ok:true}};
  }}]);
  const originalGeneration=c.transportGeneration;
  await c.receive({...request(),response_id:'source-response'});await started.promise;
  await c.receive(transition);read.resolve();await c.actionChain;
  assert.equal(c.transportGeneration,originalGeneration);assert.equal(c.turn,1);
  assert.equal(effects,0);assert.equal(sent.at(-1).result.error,'cancelled');
  // The still-connected page can receive new work after the session restarts.
  await c.receive({type:'ready',turn:1});
  await c.receive({...request('next'),response_id:'next-response'});await c.actionChain;
  assert.equal(effects,1);
});
