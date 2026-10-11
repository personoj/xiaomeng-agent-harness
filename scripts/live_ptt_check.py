"""Explicit real-cloud PTT check using existing local, synthetic speech fixtures."""
import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xiaoyun.config import ROOT, Settings
from xiaoyun.device import DeviceGate
from xiaoyun.harness import Harness
from xiaoyun.memory import SessionStore
from xiaoyun.provider import QwenProvider


def read_pcm(path):
    import numpy as np
    from scipy.signal import resample_poly
    with wave.open(str(path), 'rb') as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
        rate = wav.getframerate()
        # Streaming WAV headers may claim 0x7fffffff frames. Bound actual reads.
        raw = wav.readframes(rate * 20)
    data = np.frombuffer(raw, dtype='<i2')
    if rate != 16000:
        from math import gcd
        common = gcd(rate, 16000)
        data = np.clip(np.rint(resample_poly(data.astype(np.float64), 16000//common, rate//common)), -32768, 32767).astype('<i2')
    return data.tobytes()


async def main():
    output = ROOT/'.local/live-checks'/f'{int(time.time())}-ptt'
    output.mkdir(parents=True)
    cfg = replace(Settings.load(), data_dir=output/'sessions')
    events, sent_types = [], []
    class Observed(QwenProvider):
        async def send(self, event):
            sent_types.append(event['type'])
            await super().send(event)
    h = Harness(cfg, SessionStore(cfg.data_dir), Observed)
    async def sink(event):
        if event['type'] != 'audio.delta':events.append({'at':time.monotonic(), **event})
        elif not any(e['type']=='first_audio' and e['response_id']==event['response_id'] for e in events):
            events.append({'at':time.monotonic(), 'type':'first_audio', 'response_id':event['response_id'], 'turn':event['turn']})
    gate = DeviceGate(h, sink)
    conn = await h.attach('audio', gate.emit)
    async def wait_for(predicate, timeout=25):
        async def poll():
            while True:
                errors=[e for e in events if e['type']=='error']
                if errors:raise RuntimeError(errors[-1].get('code','provider_error'))
                for e in events:
                    if predicate(e):return e
                await asyncio.sleep(.02)
        return await asyncio.wait_for(poll(), timeout)
    async def stream(pcm):
        for offset in range(0,len(pcm),640):
            await gate.handle(conn, {'type':'input.audio','audio':base64.b64encode(pcm[offset:offset+640]).decode()})
            await asyncio.sleep(.02)
    report={'synthetic_audio':True,'hardware_test':False}
    try:
        await gate.handle(conn, {'type':'session.start','input_mode':'push_to_talk'})
        ready=next(e for e in events if e['type']=='ready')
        assert ready['turn_detection']=='manual' and ready['input_mode']=='push_to_talk'
        pcm=read_pcm(ROOT/'.local/fixtures/interrupt-shanghai.wav')
        before=sent_types.count('input_audio_buffer.append')
        await gate.handle(conn, {'type':'input.audio','audio':base64.b64encode(pcm[:640]).decode()})
        assert sent_types.count('input_audio_buffer.append')==before
        await gate.handle(conn, {'type':'input.begin'})
        await stream(pcm)
        assert not any(e['type']=='response.started' for e in events)
        release=time.monotonic()
        await gate.handle(conn, {'type':'input.commit'})
        turn=h.turn
        reply=await wait_for(lambda e:e['type']=='response.generated' and e['turn']==turn and e.get('text'))
        assert '上海' in reply['text'] and len(reply['text'])<40
        heard=await wait_for(lambda e:e['type']=='transcript.user' and e['turn']==turn)
        first=next(e for e in events if e['type']=='first_audio' and e['turn']==turn)
        before=sent_types.count('input_audio_buffer.append')
        await stream(pcm[:3200])
        assert sent_types.count('input_audio_buffer.append')==before
        await gate.handle(conn, {'type':'input.commit'})
        assert sent_types.count('input_audio_buffer.commit')==1
        report.update(manual_ready=True,no_response_before_release=True,unpressed_audio_dropped=True,
                      transcript=heard['text'],reply=reply['text'],release_to_first_audio_ms=round((first['at']-release)*1000))
        await gate.handle(conn, {'type':'input.begin'})
        await stream(read_pcm(ROOT/'.local/fixtures/ptt-exit.wav'))
        await gate.handle(conn, {'type':'input.commit'})
        await wait_for(lambda e:e['type']=='device.standby')
        await gate.stop_task
        assert not gate.awake and h.provider is None
        report.update(model_exit_tool=True,passed=True)
    except Exception as exc:
        report.update(passed=False,error=type(exc).__name__+': '+str(exc))
    finally:
        await gate.close()
        await h.detach(conn)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (output/'events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'path':str(output/'report.json'),**report},ensure_ascii=False))
    return report['passed']

if __name__=='__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
