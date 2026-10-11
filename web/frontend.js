import { VoiceClient } from './client.js';

/** Framework-independent host-page bridge. No microphone, character or business UI is injected. */
export async function connectFrontend({serverURL='http://127.0.0.1:8766',sessionId,pageId,context={},actions=[],onEvent}={}) {
  if(!sessionId||!pageId)throw new Error('sessionId and pageId are required');
  const client=new VoiceClient({baseURL:serverURL,sessionId,role:'page'});
  const narrationWaiters=new Map(),dispatchedCalls=new Set();
  const failWaiters=reason=>{
    for(const waiter of narrationWaiters.values()){clearTimeout(waiter.timer);waiter.reject(new Error(reason));}
    narrationWaiters.clear();
  };
  client.addEventListener('event',({detail:event})=>{
    if(event.type==='tool.request')dispatchedCalls.add(event.call_id);
    if(event.type==='tool.completed')dispatchedCalls.delete(event.call_id);
    if(event.type==='turn.started'||event.type==='transport.closed'){
      dispatchedCalls.clear();failWaiters(event.type);
    }
    const waiter=narrationWaiters.get(event.narration_id);
    if(waiter&&['narration.prepared','narration.failed','narration.stopped'].includes(event.type)){
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
      if(!dispatchedCalls.has(sourceCallId))return Promise.reject(new Error('narration_call_not_current'));
      if(!/^[A-Za-z0-9_-]{1,128}$/.test(narrationId||''))return Promise.reject(new Error('invalid_narration_id'));
      const bytes=pcm instanceof Uint8Array?pcm:pcm instanceof ArrayBuffer?new Uint8Array(pcm):null;
      if(!bytes||!bytes.length||bytes.length%2||bytes.length>24000*120*2)
        return Promise.reject(new Error('invalid_narration_pcm_size'));
      if(typeof text!=='string'||!text.trim()||text.length>8000)return Promise.reject(new Error('invalid_narration_text'));
      if(narrationWaiters.has(narrationId))return narrationWaiters.get(narrationId).promise;
      // Build base64 in bounded chunks; spreading a full lesson would overflow
      // JavaScript's argument stack. WAV headers must be removed by the host.
      let binary='';
      for(let offset=0;offset<bytes.length;offset+=32768)
        binary+=String.fromCharCode(...bytes.subarray(offset,offset+32768));
      let resolve,reject;
      const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
      const timer=setTimeout(()=>{
        narrationWaiters.delete(narrationId);
        try{client.send({type:'narration.cancel',narration_id:narrationId});}catch{}
        reject(new Error('narration_prepare_timeout'));
      },12000);
      narrationWaiters.set(narrationId,{promise,resolve,reject,timer});
      try{client.send({type:'narration.prepare',source_call_id:sourceCallId,narration_id:narrationId,text,pcm:btoa(binary)});}
      catch(error){clearTimeout(timer);narrationWaiters.delete(narrationId);reject(error);}
      return promise;
    },
    cancelNarration(narrationId){
      client.send({type:'narration.cancel',narration_id:narrationId});
    },
    disconnect(){failWaiters('transport.closed');client.disconnect();},
  };
}
