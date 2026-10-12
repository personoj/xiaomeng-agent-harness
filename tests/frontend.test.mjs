import test from 'node:test';
import assert from 'node:assert/strict';
import {VoiceClient} from '../web/client.js';
import {connectFrontend} from '../web/frontend.js';

async function frontend(t){
  let client;
  const sent=[];
  t.mock.method(VoiceClient.prototype,'connect',async function(){
    client=this;
    this.ws={readyState:1,send:event=>sent.push(JSON.parse(event)),close(){}};
    return this.sessionId;
  });
  const bridge=await connectFrontend({sessionId:'synthetic-session',pageId:'synthetic-page'});
  t.after(()=>bridge.disconnect());
  const event=detail=>client.notify(detail);
  event({type:'tool.request',call_id:'c1'});
  return {bridge,event,sent};
}
const recording=(overrides={})=>({sourceCallId:'c1',narrationId:'lesson_1',text:'Synthetic narration.',pcm:new Uint8Array([0,0]),...overrides});

test('source cancellation before preparation blocks late PCM and duplicate tool requests',async t=>{
  const {bridge,event,sent}=await frontend(t);
  bridge.cancelNarration('lesson_1','c1');
  assert.deepEqual(sent.at(-1),{type:'narration.cancel',narration_id:'lesson_1',source_call_id:'c1'});
  event({type:'tool.request',call_id:'c1'});
  await assert.rejects(bridge.prepareNarration(recording()),/narration_call_not_current/);
  assert.equal(sent.some(e=>e.type==='narration.prepare'),false);
});

test('cancellation immediately rejects pending preparation and ignores its late acknowledgement',async t=>{
  const {bridge,event,sent}=await frontend(t);
  const prepared=bridge.prepareNarration(recording());
  const rejected=assert.rejects(prepared,/page_cancelled/);
  bridge.cancelNarration('lesson_1','c1');
  event({type:'narration.prepared',narration_id:'lesson_1',source_call_id:'c1'});
  await rejected;
  assert.equal(sent.at(-1).source_call_id,'c1');
  await assert.rejects(bridge.prepareNarration(recording()),/narration_call_not_current/);
});

test('ID-only legacy cancellation remains compatible and binds an existing waiter when known',async t=>{
  const {bridge,sent}=await frontend(t);
  bridge.cancelNarration('legacy_playing');
  assert.deepEqual(sent.at(-1),{type:'narration.cancel',narration_id:'legacy_playing'});
  const prepared=bridge.prepareNarration(recording());
  const rejected=assert.rejects(prepared,/page_cancelled/);
  bridge.cancelNarration('lesson_1');
  await rejected;
  assert.equal(sent.at(-1).source_call_id,'c1');
});

test('old source cancellation and stopped event cannot reject a replacement source waiter',async t=>{
  const {bridge,event}=await frontend(t);
  bridge.cancelNarration('shared_id','c1');
  event({type:'tool.request',call_id:'c2'});
  const prepared=bridge.prepareNarration(recording({sourceCallId:'c2',narrationId:'shared_id'}));
  let settled=false;prepared.then(()=>{settled=true;},()=>{settled=true;});
  bridge.cancelNarration('shared_id','c1');
  event({type:'narration.stopped',narration_id:'shared_id',source_call_id:'c1',reason:'page_cancelled'});
  await Promise.resolve();
  assert.equal(settled,false);
  event({type:'narration.prepared',narration_id:'shared_id',source_call_id:'c2'});
  assert.equal((await prepared).source_call_id,'c2');
});

test('same narration ID cannot combine preparation promises from different source calls',async t=>{
  const {bridge,event}=await frontend(t);
  const first=bridge.prepareNarration(recording());
  assert.equal(bridge.prepareNarration(recording()),first);
  event({type:'tool.request',call_id:'c2'});
  await assert.rejects(bridge.prepareNarration(recording({sourceCallId:'c2'})),/narration_busy/);
  event({type:'narration.prepared',narration_id:'lesson_1',source_call_id:'c1'});
  await first;
});

test('prepare timeout sends source-bound cancellation and retires the source locally',async t=>{
  const {bridge,sent}=await frontend(t);
  let expire;
  const timer=t.mock.method(globalThis,'setTimeout',callback=>{expire=callback;return 0;});
  const prepared=bridge.prepareNarration(recording());
  const rejected=assert.rejects(prepared,/narration_prepare_timeout/);
  timer.mock.restore();
  expire();
  await rejected;
  assert.deepEqual(sent.at(-1),{type:'narration.cancel',narration_id:'lesson_1',source_call_id:'c1'});
  await assert.rejects(bridge.prepareNarration(recording()),/narration_call_not_current/);
});

for(const transition of [
  {type:'turn.started'},
  {type:'transport.closed'},
  {type:'state',state:'disconnected'},
  {type:'state',state:'error'},
])test(`${transition.type} ${transition.state||''} retires pending bridge preparation`,async t=>{
  const {bridge,event}=await frontend(t);
  const prepared=bridge.prepareNarration(recording());
  const rejected=assert.rejects(prepared,new RegExp(transition.type.replace('.','\\.')));
  event(transition);
  await rejected;
  await assert.rejects(bridge.prepareNarration(recording()),/narration_call_not_current/);
});
