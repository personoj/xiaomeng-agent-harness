import { VoiceClient } from './client.js';

/** Framework-independent host-page bridge. No microphone, character or business UI is injected. */
export async function connectFrontend({serverURL='http://127.0.0.1:8766',sessionId,pageId,context={},actions=[],onEvent}={}) {
  if(!sessionId||!pageId)throw new Error('sessionId and pageId are required');
  const client=new VoiceClient({baseURL:serverURL,sessionId,role:'page'});
  const narrationWaiters=new Map(),dispatchedCalls=new Map();
  const failWaiters=reason=>{
    for(const waiter of narrationWaiters.values()){clearTimeout(waiter.timer);waiter.reject(new Error(reason));}
    narrationWaiters.clear();
  };
  const cancelNarration=(narrationId,sourceCallId,reason='page_cancelled')=>{
    const waiter=narrationWaiters.get(narrationId);
    const callId=sourceCallId??waiter?.sourceCallId;
    if(callId!==undefined&&dispatchedCalls.has(callId))dispatchedCalls.set(callId,false);
    if(waiter&&(callId===undefined||waiter.sourceCallId===callId)){
      clearTimeout(waiter.timer);narrationWaiters.delete(narrationId);waiter.reject(new Error(reason));
    }
    client.send({type:'narration.cancel',narration_id:narrationId,
      ...(callId!==undefined?{source_call_id:callId}:{})});
  };
  client.addEventListener('event',({detail:event})=>{
    if(event.type==='tool.request'&&!dispatchedCalls.has(event.call_id))dispatchedCalls.set(event.call_id,true);
    if(event.type==='tool.completed')dispatchedCalls.delete(event.call_id);
    if(event.type==='turn.started'||event.type==='transport.closed'
        ||event.type==='state'&&['disconnected','error'].includes(event.state)){
      dispatchedCalls.clear();failWaiters(event.type);
    }
    const waiter=narrationWaiters.get(event.narration_id);
    if(waiter&&(!event.source_call_id||event.source_call_id===waiter.sourceCallId)
        &&['narration.prepared','narration.failed','narration.stopped'].includes(event.type)){
      clearTimeout(waiter.timer);narrationWaiters.delete(event.narration_id);
      if(event.type==='narration.prepared')waiter.resolve(event);
      else waiter.reject(new Error(event.code||event.reason||event.type));
    }
    onEvent?.(event);
  });
  await client.connect();
  client.registerActions(actions);client.updateContext(context,pageId);
  return {
    sessionId:client.sessionId,
    updateContext(snapshot){client.updateContext(snapshot,pageId);},
    setActions(nextActions){client.registerActions(nextActions);},
    reportTask({taskId,sourceCallId,status,result}){client.send({type:'task.update',task_id:taskId,source_call_id:sourceCallId,status,result});},
    prepareNarration({sourceCallId,narrationId,text,pcm}){
      if(!dispatchedCalls.get(sourceCallId))return Promise.reject(new Error('narration_call_not_current'));
      if(!/^[A-Za-z0-9_-]{1,128}$/.test(narrationId||''))return Promise.reject(new Error('invalid_narration_id'));
      const bytes=pcm instanceof Uint8Array?pcm:pcm instanceof ArrayBuffer?new Uint8Array(pcm):null;
      if(!bytes||!bytes.length||bytes.length%2||bytes.length>24000*120*2)
        return Promise.reject(new Error('invalid_narration_pcm_size'));
      if(typeof text!=='string'||!text.trim()||text.length>8000)return Promise.reject(new Error('invalid_narration_text'));
      if(narrationWaiters.has(narrationId)){
        const waiter=narrationWaiters.get(narrationId);
        return waiter.sourceCallId===sourceCallId?waiter.promise:Promise.reject(new Error('narration_busy'));
      }
      // Build base64 in bounded chunks; spreading a full lesson would overflow
      // JavaScript's argument stack. WAV headers must be removed by the host.
      let binary='';
      for(let offset=0;offset<bytes.length;offset+=32768)
        binary+=String.fromCharCode(...bytes.subarray(offset,offset+32768));
      let resolve,reject;
      const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
      const timer=setTimeout(()=>{
        try{cancelNarration(narrationId,sourceCallId,'narration_prepare_timeout');}catch{}
      },12000);
      narrationWaiters.set(narrationId,{promise,resolve,reject,timer,sourceCallId});
      try{client.send({type:'narration.prepare',source_call_id:sourceCallId,narration_id:narrationId,text,pcm:btoa(binary)});}
      catch(error){clearTimeout(timer);narrationWaiters.delete(narrationId);reject(error);}
      return promise;
    },
    cancelNarration,
    disconnect(){failWaiters('transport.closed');client.disconnect();},
  };
}
