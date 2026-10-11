import { VoiceClient } from './client.js';
import { XiaoyunCharacter } from './character.js';
import { icon } from './icons.js';

const $ = id => document.getElementById(id);
for (const element of document.querySelectorAll('[data-icon]')) element.innerHTML = icon(element.dataset.icon);
const character = new XiaoyunCharacter($('xiaoyun'));
const messages = new Map(), hiddenMessages = new Set();
const bars = Array.from({length:46}, () => { const bar=document.createElement('i'); $('waveform').append(bar); return bar; });
let client=null, ready=false, connecting=false, connectionPromise=null, currentState='disconnected';
let count=0, toastTimer, lastVisual='', lastMicLabel='', animationFrame, lastAnimationTime=0;
let integration={page_connected:false,tools:[],context:{},revision:0};
const sessionKey='xiaoyun.session';
const pairedSession=new URLSearchParams(location.search).get('session_id');
if(pairedSession&&/^[a-f0-9]{32}$/.test(pairedSession))localStorage.setItem(sessionKey,pairedSession);

function toast(text) {
  clearTimeout(toastTimer);$('toast').textContent=text;$('toast').hidden=false;
  toastTimer=setTimeout(()=>$('toast').hidden=true,4200);
}
function log(event) {
  const data=Object.fromEntries(Object.entries(event).filter(([k])=>!['session_id','audio'].includes(k)));
  const lines=$('events').textContent.split('\n');
  lines.push(new Date().toLocaleTimeString()+' '+JSON.stringify(data));
  $('events').textContent=lines.slice(-100).join('\n');$('events').scrollTop=$('events').scrollHeight;
}
function renderIntegration(value) {
  integration=value;
  $('frontend-status').textContent=value.page_connected?'前端已接入':'尚未接入前端';
  $('frontend-description').textContent=value.page_connected?(value.page_id||'前端连接中…'):'前端连接后，这里会展示它提供的工具和上下文。';
  $('context').textContent=JSON.stringify(value.context||{},null,2);
  $('context-revision').textContent='版本 '+(value.revision||0);
  const tools=value.tools||[];
  const external=tools.filter(t=>t.source==='page').length;
  $('tool-count').textContent=external;$('tool-count').hidden=!external;
  $('tools-summary').textContent=tools.length+' 个';
  $('tool-list').replaceChildren();
  for(const tool of tools){
    const row=document.createElement('div');row.className='tool-row';
    const title=document.createElement('strong');title.textContent=tool.name;
    const description=document.createElement('p');description.textContent=tool.description||'由前端注册';
    row.append(title,description);$('tool-list').append(row);
  }
  if(!tools.length){const note=document.createElement('p');note.className='detail-note';note.textContent='开始会话后显示可用工具。';$('tool-list').append(note);}
}
function updateControls() {
  $('mic').disabled=connecting;
  const label=connecting?'连接中…':client?.stream?'麦克风已开启':ready?'开启麦克风':'开启语音';
  if(label!==lastMicLabel){$('mic-label').textContent=label;lastMicLabel=label;}
  $('end-session').disabled=!ready;
  $('send').disabled=connecting||!$('text').value.trim();
  const sid=client?.sessionId||localStorage.getItem(sessionKey);
  $('session-id').textContent=sid||'开始对话后生成';$('settings-session').textContent=sid||'尚未开始';
}
function message(id, role, text='', append=false) {
  if(hiddenMessages.has(id)) return;
  if(!messages.has(id)){
    $('empty-state')?.remove();
    const row=document.createElement('div');row.className='message '+role;row.dataset.messageId=id;
    const avatar=document.createElement('div');avatar.className='message-avatar';
    avatar.innerHTML=role==='user'?icon('user'):'<img src="/static/xiaoyun-avatar.svg" alt="小萌">';
    const content=document.createElement('div');content.className='message-content';
    const bubble=document.createElement('div');bubble.className='bubble';
    const time=document.createElement('time');time.className='message-time';time.textContent=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});time.dateTime=new Date().toISOString();
    content.append(bubble,time);row.append(avatar,content);$('transcripts').append(row);
    messages.set(id,{row,bubble});
  }
  const record=messages.get(id);record.bubble.textContent=append?record.bubble.textContent+text:text;
  $('transcripts').scrollTop=$('transcripts').scrollHeight;
  return record;
}
function toolMessage(event){
  const id='tool-'+event.call_id;
  if(hiddenMessages.has(id))return;
  const record=message(id,'assistant','');
  if(!record)return;
  record.row.classList.add('message-tool');
  const ok=event.status==='succeeded';
  record.row.classList.toggle('failed',!ok);
  const marker=document.createElement('span');marker.className='tool-status-icon';marker.innerHTML=icon(ok?'check':'clock');
  const text=document.createElement('span');
  const definition=integration.tools.find(tool=>tool.name===event.name);
  const label=(definition?.description?.split('。')[0]||event.name);
  text.textContent=(ok?(event.result?.status==='accepted'?'任务已受理':'工具调用完成'):'工具结果待确认')+' · '+(label.length>40?label.slice(0,40)+'…':label);
  const detail=document.createElement('button');detail.className='tool-detail-trigger';detail.setAttribute('aria-label','查看工具执行记录');detail.innerHTML=icon('chevron');detail.onclick=()=>showDetails('events');
  record.bubble.replaceChildren(marker,text,detail);
}
function showDetails(section){
  const names={tools:'工具与上下文',events:'运行记录',model:'会话设置'};
  $('details-title').textContent=names[section];
  for(const name of Object.keys(names))$(name+'-content').hidden=name!==section;
  if(!$('details').open)$('details').showModal();
}
function clearDisplay(){
  for(const id of messages.keys())hiddenMessages.add(id);
  messages.clear();$('transcripts').replaceChildren();
  const empty=document.createElement('div');empty.id='empty-state';empty.className='empty-state';
  empty.innerHTML='<img src="/static/xiaoyun-avatar.svg" alt=""><h3>小萌准备好了</h3><p>说出你的想法，我们从这里开始。</p>';
  $('transcripts').append(empty);
}
async function ensureConnected(){
  if(ready&&client?.ws?.readyState===WebSocket.OPEN)return client;
  if(connectionPromise)return connectionPromise;
  connectionPromise=(async()=>{
    connecting=true;updateControls();
    if(client){
      const old=client;client=null;
      if(old.ws?.readyState===WebSocket.OPEN)
        await new Promise(resolve=>{old.ws.addEventListener('close',resolve,{once:true});old.disconnect();});
      else old.disconnect();
    }
    const instance=new VoiceClient({sessionId:localStorage.getItem(sessionKey)||undefined,role:'audio'});
    client=instance;
    let resolveReady,rejectReady;
    const modelReady=new Promise((resolve,reject)=>{resolveReady=resolve;rejectReady=reject;});
    // A channel error can arrive before connect() settles; preserve rejection for the later await.
    modelReady.catch(()=>{});
    // Attach the handler before connecting so no early state or integration event is lost.
    instance.addEventListener('event',({detail:e})=>{
      if(client!==instance)return;
      if(!['audio.delta','transcript.delta','playback.completed'].includes(e.type))log(e);
      if(e.type==='state'){
        currentState=e.state;
        if(['disconnected','error'].includes(e.state)){ready=false;if(!connecting)updateControls();}
      }
      if(e.type==='ready'){ready=true;resolveReady(instance);}
      if(e.type==='integration.updated')renderIntegration(e.integration);
      if(e.type==='metrics')$('latency').textContent=e.first_audio_ms+' ms';
      if(e.type==='tool.completed'){$('calls').textContent=(++count)+' 次';toolMessage(e);}
      if(e.type==='transcript.user')message('user-'+e.turn,'user',e.text);
      if(e.type==='transcript.delta')message(e.response_id,'assistant',e.delta,true);
      if(e.type==='response.generated'&&e.text)message(e.response_id,'assistant',e.text);
      if(e.type==='session.idle')toast(e.message);
      if(e.type==='error'){toast(e.message||e.code);if(connecting)rejectReady(new Error(e.message||e.code));}
      updateControls();
    });
    let timeout;
    try{
      await instance.prepareAudio();
      const id=await instance.connect();localStorage.setItem(sessionKey,id);updateControls();
      // This console owns only audio. Business tools and context come from independently connected frontends.
      instance.start();
      timeout=setTimeout(()=>rejectReady(new Error('模型连接超时，请重试。')),25000);
      return await modelReady;
    }catch(error){
      if(client===instance){ready=false;currentState='disconnected';instance.disconnect();}
      throw error;
    }finally{clearTimeout(timeout);connecting=false;updateControls();}
  })().finally(()=>{connectionPromise=null;});
  return connectionPromise;
}
async function sendText(text){
  const value=text.trim();if(!value)return;
  try{const active=await ensureConnected();await active.prepareAudio();active.submitText(value);}
  catch(error){toast(error.message);}
  updateControls();
}
$('mic').onclick=async()=>{
  try{
    const active=await ensureConnected();
    if(active.stream)active.stopMic();else await active.startMic();
  }catch(error){toast(error.name==='NotAllowedError'?'尚未获得麦克风权限，你仍可以使用文字对话。':error.message);}
  updateControls();
};
$('end-session').onclick=()=>{client?.disconnect();ready=false;currentState='disconnected';renderIntegration({page_connected:false,tools:[],context:{},revision:0});updateControls();};
function interrupt(){if(ready)client.interrupt();}
$('interrupt').onclick=interrupt;
$('character-stage').onclick=()=>{if(client?.isPlaying())interrupt();};
$('text-form').onsubmit=e=>{e.preventDefault();const value=$('text').value;$('text').value='';sendText(value);};
$('text').oninput=updateControls;
$('clear-chat').onclick=()=>{clearDisplay();toast('已清空显示，会话记忆仍保留。');};
$('new-session').onclick=()=>{
  const previous=client;client=null;previous?.disconnect();localStorage.removeItem(sessionKey);
  ready=false;currentState='disconnected';clearDisplay();hiddenMessages.clear();
  renderIntegration({page_connected:false,tools:[],context:{},revision:0});updateControls();
  $('details').close();toast('下次对话将使用新的会话。');
};
$('settings').onclick=()=>showDetails('model');
$('open-tools').onclick=()=>showDetails('tools');
$('open-events').onclick=()=>showDetails('events');
$('open-model').onclick=()=>showDetails('model');
$('close-details').onclick=()=>$('details').close();
$('details').addEventListener('click',e=>{if(e.target===$('details')){const r=$('details').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$('details').close();}});
$('copy-session').onclick=async()=>{const id=client?.sessionId||localStorage.getItem(sessionKey);if(!id){toast('开始一次对话后即可获取会话 ID。');return;}try{await navigator.clipboard.writeText(id);toast('会话 ID 已复制。');}catch{toast('请在会话信息中手动复制 ID。');}};
$('clear-log').onclick=()=>$('events').textContent='';
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('details').open)interrupt();});
function animate(now){
  animationFrame=requestAnimationFrame(animate);
  if(document.hidden||now-lastAnimationTime<32)return;lastAnimationTime=now;
  const speaking=!!client?.isPlaying(),mic=!!client?.stream;
  const levels=client?.audioLevels()||{input:0,output:0};
  const state=speaking?'speaking':['connecting','thinking','working'].includes(currentState)?'thinking':mic&&ready?'listening':'idle';
  character.update(state,state==='speaking'?levels.output:0);
  const visualKey=state+':'+ready+':'+mic;
  if(visualKey!==lastVisual){
    lastVisual=visualKey;$('xiaoyun').dataset.state=state;
    $('voice-status').classList.toggle('idle',state==='idle');
    $('state').textContent={speaking:'正在说话',thinking:'正在思考',listening:'正在倾听',idle:ready?'随时可以开始':'等待与你对话'}[state];
    $('voice-hint').textContent=speaking?'直接开口，或点击小萌打断回答':mic?'小萌正在听，说话时也可以随时插话':'开启麦克风，随时与小萌交流';
    $('character-stage').classList.toggle('can-interrupt',speaking);$('interrupt').hidden=!speaking;
  }
  const level=speaking?levels.output:mic?levels.input:0;
  $('waveform').classList.toggle('quiet',level<.015);
  bars.forEach((bar,i)=>{const shape=.3+.7*Math.abs(Math.sin(i*.74+now*.0018));const envelope=.35+.65*Math.sin((i+1)/47*Math.PI);bar.style.height=(5+level*42*shape*envelope)+'px';});
}
animationFrame=requestAnimationFrame(animate);
window.addEventListener('pagehide',()=>{cancelAnimationFrame(animationFrame);character.destroy();client?.disconnect();});
fetch('/api/health').then(r=>{if(!r.ok)throw Error();return r.json();}).then(data=>{
  $('local-status').classList.add('online');$('local-status').lastElementChild.textContent='本地已连接';
  $('model').textContent=data.model;$('voice').textContent=data.voice;$('turn-detection').textContent=data.turn_detection;
}).catch(()=>{$('local-status').lastElementChild.textContent='本地服务未连接';});
renderIntegration(integration);updateControls();
