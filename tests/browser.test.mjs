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
  c.ws={readyState:1,send:event=>sent.push(JSON.parse(event))};
  c.audio={state:'running',currentTime:0,destination:{},createBuffer:(_,length)=>({duration:length/24000,getChannelData:()=>new Float32Array(length)}),createBufferSource:()=>{const s={connect(){},start(at){this.at=at;},stop(){this.stopped=true;}};nodes.push(s);return s;}};
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
