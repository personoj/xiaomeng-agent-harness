/** Transport-neutral page/audio client. Never receives cloud credentials. */
export class VoiceClient extends EventTarget {
  constructor({ baseURL = location.origin, sessionId, role = 'duplex' } = {}) {
    super();
    Object.assign(this, { baseURL, sessionId, role });
    this.actions = new Map(); this.completed = new Map(); this.playing = new Map();
    this.cancelled = new Set(); this.revision = 0; this.context = {};
    this.turn = null; this.transportGeneration = 0; this.intentGeneration = 0;
    this.actionChain = Promise.resolve(); this.nextAt = 0;
  }
  notify(event) { this.dispatchEvent(new CustomEvent('event', { detail: event })); }
  send(event) {
    if (this.ws?.readyState !== WebSocket.OPEN) throw new Error('连接未就绪');
    this.ws.send(JSON.stringify(event));
  }
  async prepareAudio() {
    if (!this.audio) this.audio = new AudioContext();
    if (!this.outputAnalyser) {
      this.outputAnalyser=this.audio.createAnalyser();this.outputAnalyser.fftSize=256;
      this.outputAnalyser.connect(this.audio.destination);this.outputSamples=new Float32Array(256);
    }
    await this.audio.resume();
  }
  async connect() {
    const generation = ++this.transportGeneration;
    this.turn = null;
    const response = await fetch(`${this.baseURL}/api/sessions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: this.sessionId }),
    });
    if (!response.ok) throw new Error('创建会话失败');
    const data = await response.json();
    if (generation !== this.transportGeneration) throw new Error('连接已取消');
    this.sessionId = data.session_id;
    const url = new URL(`/ws/${this.sessionId}?role=${this.role}`, this.baseURL);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = this.ws = new WebSocket(url);
    let resolveAttached, rejectAttached, attached=false;
    const attachment = new Promise((resolve,reject)=>{resolveAttached=resolve;rejectAttached=reject;});
    const deadline=setTimeout(()=>rejectAttached(new Error('会话通道连接超时')),10000);
    socket.onerror=()=>rejectAttached(new Error('连接本地服务失败'));
    socket.onmessage = ({ data }) => {
      if (generation !== this.transportGeneration) return;
      const event = JSON.parse(data);
      if(event.type==='attached'){attached=true;resolveAttached();}
      if(event.type==='error'&&!attached)rejectAttached(new Error(event.message||event.code));
      this.receive(event).catch(error => this.notify({ type: 'error', message: error.message }));
    };
    socket.onclose = () => {
      if(!attached)rejectAttached(new Error('会话通道未能连接'));
      if (generation !== this.transportGeneration) return;
      this.transportGeneration++;
      this.stopMic(); this.clearAudio([...this.playing.keys()], false);
      clearInterval(this.progressTimer);
      this.notify({ type: 'state', state: 'disconnected' });
      this.notify({ type: 'transport.closed' });
    };
    try{
      await attachment;
      if (generation !== this.transportGeneration) throw new Error('连接已取消');
    }catch(error){socket.close();throw error;}finally{clearTimeout(deadline);}
    this.progressTimer = setInterval(() => this.reportProgress(), 100);
    return this.sessionId;
  }
  registerActions(actions) {
    this.actions = new Map(actions.map(action => [action.name, action]));
    this.send({ type: 'tools.register', tools: actions.map(({ execute, ...spec }) => spec) });
  }
  updateContext(context, pageId = 'development') {
    this.context = structuredClone(context);
    this.send({ type: 'context.update', context, page_id: pageId });
  }
  start() { this.send({ type: 'session.start' }); }
  submitText(text) { this.send({ type: 'input.text', text }); }
  interrupt() {
    this.clearAudio([...this.playing.keys()]);
    this.send({ type: 'interrupt' });
  }
  async startMic() {
    await this.prepareAudio();
    if (this.stream) return;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1,
    }});
    try {
      if (!this.modelReady || this.ws?.readyState !== WebSocket.OPEN) throw new Error('模型已断开，请重新连接后开启麦克风。');
      if (!this.workletLoaded) {
        await this.audio.audioWorklet.addModule(new URL('/static/audio-worklet.js', this.baseURL));
        this.workletLoaded = true;
      }
      this.stream = stream;
      this.source = this.audio.createMediaStreamSource(stream);
      this.inputAnalyser=this.audio.createAnalyser();this.inputAnalyser.fftSize=256;
      this.inputSamples=new Float32Array(256);this.source.connect(this.inputAnalyser);
      this.capture = new AudioWorkletNode(this.audio, 'microphone-pcm');
      this.capture.port.onmessage = ({ data }) => {
        if (this.ws?.readyState !== WebSocket.OPEN) return;
        if (this.ws.bufferedAmount > 256000) {
          this.stopMic();
          this.notify({ type: 'error', message: '发送音频积压，麦克风已停止。请重连。' });
          return;
        }
        this.send({ type: 'input.audio', audio: btoa(String.fromCharCode(...new Uint8Array(data))) });
      };
      this.mute = this.audio.createGain(); this.mute.gain.value = 0;
      this.source.connect(this.capture).connect(this.mute).connect(this.audio.destination);
    } catch (error) {
      stream.getTracks().forEach(track => track.stop()); throw error;
    }
  }
  stopMic() {
    this.stream?.getTracks().forEach(track => track.stop()); this.stream = null;
    this.source?.disconnect(); this.capture?.disconnect(); this.mute?.disconnect();
    this.inputAnalyser=null;
  }
  isPlaying() {
    if(!this.audio)return false;
    const now=this.audio.currentTime;
    return [...this.playing.values()].some(p=>p.nodes.some(n=>!n.finished&&now>=n.start&&now<n.start+n.samples/24000));
  }
  audioLevels() {
    const rms=(analyser,samples)=>{
      if(!analyser||!samples)return 0;analyser.getFloatTimeDomainData(samples);
      return Math.min(1,Math.sqrt(samples.reduce((sum,x)=>sum+x*x,0)/samples.length)*5);
    };
    return {input:rms(this.inputAnalyser,this.inputSamples),output:rms(this.outputAnalyser,this.outputSamples)};
  }
  playback(id) {
    if (!this.playing.has(id)) this.playing.set(id, { id, nodes: [], samples: 0, lastSeq: 0, ended: false });
    return this.playing.get(id);
  }
  played(p) {
    if (!this.audio) return 0;
    return Math.min(p.samples, p.nodes.reduce((n, node) => n + Math.min(node.samples, Math.max(0, Math.floor((this.audio.currentTime - node.start) * 24000))), 0));
  }
  reportProgress() {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    for (const [id, p] of this.playing) {
      const samples = this.played(p);
      const drained = p.ended && samples === p.samples && p.nodes.every(node => node.finished);
      this.send({ type: 'playback.progress', response_id: id, played_samples: samples, drained });
      if (drained) this.playing.delete(id);
    }
  }
  clearAudio(ids, acknowledge = true) {
    for (const id of ids) {
      this.cancelled.add(id);
      const p = this.playing.get(id);
      if (!p) continue;
      const samples = this.played(p);
      if (acknowledge && this.ws?.readyState === WebSocket.OPEN)
        this.send({ type: 'playback.progress', response_id: id, played_samples: samples, drained: false });
      this.playing.delete(id);
      for (const node of p.nodes) { try { node.source.stop(); } catch {} }
    }
    this.nextAt = this.audio?.currentTime || 0;
  }
  async receive(event) {
    // Late durable tool results may carry an older turn. They must not make a
    // superseded intent current again, even if the page revision is unchanged.
    if (Number.isInteger(event.turn)) this.turn = Math.max(this.turn ?? event.turn, event.turn);
    if (event.type === 'ready') this.modelReady = true;
    if (event.type === 'state' && ['disconnected', 'error'].includes(event.state)) {
      // Session stop can leave the page WebSocket open and the turn unchanged.
      this.intentGeneration++; this.modelReady = false; this.stopMic();
    }
    if (event.revision !== undefined) this.revision = event.revision;
    if (event.type === 'context.updated') this.context = event.context;
    if (event.type === 'tool.request') {
      const generation = this.transportGeneration, turn = event.turn ?? this.turn, intent = this.intentGeneration;
      this.actionChain = this.actionChain.then(() => this.execute(event, generation, turn, intent)).catch(error => this.notify({ type: 'error', message: error.message }));
    } else if (event.type === 'audio.clear') {
      this.clearAudio(event.response_ids);
    } else if (event.type === 'audio.delta' && !this.cancelled.has(event.response_id)) {
      if (!this.audio || this.audio.state !== 'running') {
        this.notify({ type: 'error', message: '浏览器尚未允许播放，请点击连接按钮启用声音。' }); return;
      }
      const p = this.playback(event.response_id);
      if (event.seq <= p.lastSeq) return;
      p.lastSeq = event.seq;
      const bytes = Uint8Array.from(atob(event.audio), c => c.charCodeAt(0));
      const pcm = new DataView(bytes.buffer);
      const buffer = this.audio.createBuffer(1, bytes.length / 2, 24000);
      const channel = buffer.getChannelData(0);
      for (let i = 0; i < channel.length; i++) channel[i] = pcm.getInt16(i * 2, true) / 32768;
      const source = this.audio.createBufferSource(); source.buffer = buffer; source.connect(this.outputAnalyser||this.audio.destination);
      const start = Math.max(this.nextAt, this.audio.currentTime + 0.025);
      const node = { source, start, samples: channel.length, finished: false };
      source.onended = () => { node.finished = true; this.reportProgress(); };
      p.nodes.push(node); p.samples += channel.length;
      this.nextAt = start + buffer.duration; source.start(start);
    } else if (event.type === 'audio.end' && !this.cancelled.has(event.response_id)) {
      this.playback(event.response_id).ended = true;
      this.reportProgress();
    }
    this.notify(event);
  }
  async execute(request, generation = this.transportGeneration, turn = request.turn ?? this.turn, intent = this.intentGeneration) {
    const { call_id, name, args, expected_revision } = request;
    if (this.completed.has(call_id)) {
      if (generation === this.transportGeneration) this.send(this.completed.get(call_id));
      return;
    }
    const action = this.actions.get(name);
    const isCurrent = () => this.revision === expected_revision
      && generation === this.transportGeneration && turn === this.turn
      && intent === this.intentGeneration && !this.cancelled.has(request.response_id);
    let reply;
    if (!action || !isCurrent()) {
      const error = !action ? 'unknown_tool' : generation !== this.transportGeneration
        ? 'transport_expired' : turn !== this.turn ? 'turn_superseded'
        : intent !== this.intentGeneration ? 'session_stopped'
        : this.cancelled.has(request.response_id) ? 'response_cancelled' : 'page_context_changed';
      reply = { type: 'tool.result', call_id, result: { error } };
    } else {
      try {
        const output = await action.execute(args, {
          callId: call_id, contextAtStart: structuredClone(this.context),
          isCurrent,
        });
        reply = { type: 'tool.result', call_id, result: output?.result ?? output ?? {} };
        if (output?.context && isCurrent()) {
          reply.context = output.context; this.context = output.context; this.revision++;
        }
      } catch {
        reply = { type: 'tool.result', call_id, result: { error: 'page_action_failed', outcome: 'unknown' } };
      }
    }
    // Preserve the outcome of an already-submitted mutation, but never send an
    // old transport's result through a replacement connection.
    this.completed.set(call_id, reply);
    if (generation === this.transportGeneration) this.send(reply);
  }
  disconnect() {
    this.transportGeneration++;
    this.stopMic(); this.clearAudio([...this.playing.keys()]);
    clearInterval(this.progressTimer); this.ws?.close();
    this.audio?.close();
    this.modelReady = false;
    this.notify({ type: 'state', state: 'disconnected' });
    this.notify({ type: 'transport.closed' });
  }
}
