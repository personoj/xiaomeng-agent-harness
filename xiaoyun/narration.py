"""Call-bound prerecorded speech on the existing acknowledged audio transport.

All methods run under Harness.lock. PCM remains ephemeral, and the send window
advances only when the audio client confirms played samples.
"""
import base64
import binascii
from dataclasses import dataclass
import hashlib
import re
import uuid

RATE = 24000
MAX_SAMPLES = RATE * 120
AHEAD_SAMPLES = RATE * 3 // 4
CHUNK_SAMPLES = RATE // 10


@dataclass
class PreparedNarration:
    id: str
    call_id: str
    connection: str
    turn: int
    text: str
    pcm: bytes
    digest: str
    accepted: bool = False
    committed: bool = False
    response_id: str | None = None
    started: bool = False

    @property
    def samples(self):
        return len(self.pcm) // 2


class Narration:
    def __init__(self, harness):
        self.h = harness
        self.current: PreparedNarration | None = None
        self.cancelled_calls = set()

    def reset_cancellations(self):
        # Old turns/session responses cannot prepare or continue. Retain only
        # this scope's call IDs, bounded by Harness's per-turn tool budget.
        self.cancelled_calls.clear()

    def _owns_call(self, connection, call_id):
        h = self.h
        call = h.store.data["calls"].get(call_id)
        response = h.responses.get(call.get("response_id")) if call else None
        return bool(h.provider and h.channels.get("page", (None,))[0] == connection
                    and call and call.get("connection") == connection
                    and call.get("page_id") == h.page_id and call.get("turn") == h.turn
                    and response and not response.interrupted and call_id in response.calls)

    async def prepare(self, connection, event):
        """A rejection is correlated for the SDK, without echoing text/PCM."""
        narration_id = event.get("narration_id")
        try:
            await self._prepare(connection, event)
        except (ValueError, TypeError, binascii.Error) as exc:
            code = str(exc) if isinstance(exc, ValueError) else "invalid_narration"
            await self.h.emit({"type": "narration.failed", "narration_id": narration_id,
                               "code": code[:100]}, "page")

    async def _prepare(self, connection, event):
        h = self.h
        nid, call_id, text, encoded = (event.get(k) for k in ("narration_id", "source_call_id", "text", "pcm"))
        if not isinstance(nid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", nid):
            raise ValueError("invalid_narration_id")
        if not isinstance(call_id, str):
            raise ValueError("invalid_narration_call")
        if call_id in self.cancelled_calls:
            raise ValueError("narration_call_cancelled")
        call = h.store.data["calls"].get(call_id)
        response = h.responses.get(call.get("response_id")) if call else None
        if (not call or call.get("status") != "dispatched" or call.get("connection") != connection
                or call.get("page_id") != h.page_id or call.get("turn") != h.turn
                or not response or response.interrupted or call_id not in response.calls):
            raise ValueError("narration_call_not_current")
        if not h.provider or "audio" not in h.channels:
            raise ValueError("narration_audio_unavailable")
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError("invalid_narration_text")
        if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_SAMPLES * 8 // 3 + 4:
            raise ValueError("invalid_narration_pcm_size")
        pcm = base64.b64decode(encoded, validate=True)
        if len(pcm) % 2 or not 1 <= len(pcm)//2 <= MAX_SAMPLES:
            raise ValueError("invalid_narration_pcm_size")
        digest = hashlib.sha256(pcm).hexdigest()
        old = self.current
        if old:
            if (old.id, old.call_id, old.connection, old.text, old.digest) != (nid, call_id, connection, text, digest):
                raise ValueError("narration_busy")
        else:
            self.current = PreparedNarration(nid, call_id, connection, h.turn, text, pcm, digest)
        await h.emit({"type": "narration.prepared", "narration_id": nid, "source_call_id": call_id,
                      "duration": len(pcm)/(2*RATE), "duplicate": old is not None}, "page")

    async def tool_completed(self, call_id, status, result):
        item = self.current
        if not item or item.call_id != call_id:
            return
        if status == "succeeded" and result.get("accepted") is True and result.get("narration_id") == item.id:
            item.accepted = True
        else:
            await self.cancel("tool_not_accepted")

    async def consume(self, source_response):
        item = self.current
        if (not item or not item.accepted or item.call_id not in source_response.calls
                or item.turn != self.h.turn or source_response.interrupted):
            # Cancelled narration must not trigger a model reread, but it must
            # not swallow another call's result or a newer accepted narration.
            return bool(source_response.calls) and source_response.calls <= self.cancelled_calls
        item.committed = True
        # Reserve a response while waiting for previous speech to drain. Other
        # task notifications must see an unfinished response and stay queued.
        from .harness import Response
        rid = "narration_" + uuid.uuid4().hex
        item.response_id = rid
        self.h.responses[rid] = Response(rid, item.turn, self.h.revision, text=item.text, narration_id=item.id)
        await self.try_start()
        return True

    async def try_start(self):
        h, item = self.h, self.current
        if not item or not item.committed or item.started:
            return
        if (item.turn != h.turn or not h.provider or "audio" not in h.channels
                or h.channels.get("page", (None,))[0] != item.connection):
            await self.cancel("channel_or_turn_changed")
            return
        # A full sample count alone is not a drain acknowledgement.
        if h.input_open or h.speech_active or h.expected_turns or any(
                not r.interrupted and (not r.done or (r.generated_samples and not r.drained))
                for r in h.responses.values() if r.id != item.response_id):
            return
        rid = item.response_id
        item.started = True
        await h.emit({"type": "response.started", "response_id": rid})
        await h.emit({"type": "narration.started", "narration_id": item.id,
                      "response_id": rid, "duration": item.samples/RATE}, "page")
        await self.pump()

    async def pump(self):
        h, item = self.h, self.current
        if not item or not item.response_id or not item.started:
            return
        response = h.responses[item.response_id]
        if response.interrupted or response.done:
            return
        limit = min(item.samples, response.played_samples + AHEAD_SAMPLES)
        while response.generated_samples < limit:
            end = min(limit, response.generated_samples + CHUNK_SAMPLES)
            raw = item.pcm[response.generated_samples*2:end*2]
            response.generated_samples = end
            response.sequence += 1
            response.envelope.append(raw)
            await h.set_state("speaking")
            await h.emit({"type": "audio.delta", "response_id": response.id, "seq": response.sequence,
                          "sample_rate": RATE, "audio": base64.b64encode(raw).decode()}, "audio")
        if response.generated_samples == item.samples:
            response.done = True
            await h.emit({"type": "audio.end", "response_id": response.id, "samples": item.samples}, "audio")
            # Full text is persisted only by Harness._playback after a real drain.
            await h.emit({"type": "response.generated", "response_id": response.id,
                          "narration_id": item.id, "status": "completed"})

    async def playback(self, response, completed):
        item = self.current
        if not item or item.response_id != response.id or response.interrupted:
            await self.try_start()
            return
        await self.h.emit({"type": "narration.progress", "narration_id": item.id,
                           "response_id": response.id, "played_seconds": response.played_samples/RATE,
                           "total_seconds": item.samples/RATE, "completed": completed}, "page")
        if completed:
            self.current = None
        else:
            await self.pump()

    async def cancel_from_page(self, connection, event):
        h = self.h
        nid, call_id = event.get("narration_id"), event.get("source_call_id")
        if not isinstance(nid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", nid):
            raise ValueError("invalid_narration_id")
        source_bound = "source_call_id" in event
        if source_bound and (not isinstance(call_id, str) or not call_id):
            raise ValueError("invalid_narration_call")
        item = self.current
        if item and item.id == nid:
            if item.connection != connection:
                raise ValueError("narration_not_owned_by_page")
            if source_bound and item.call_id != call_id:
                return
            await self.cancel("page_cancelled")
            return
        # Legacy ID-only cancellation still applies to prepared/playing audio.
        # Before prepare there is no PCM owner yet: authenticate the source
        # call instead, and retire only that intent without clearing any audio.
        if not source_bound or not self._owns_call(connection, call_id):
            return
        if item and item.call_id == call_id:  # A different ID is not this recording.
            return
        call = h.store.data["calls"][call_id]
        if call.get("status") != "dispatched":
            return
        self.cancelled_calls.add(call_id)
        await h.emit({"type": "narration.stopped", "narration_id": nid,
                      "source_call_id": call_id, "response_id": None, "reason": "page_cancelled"}, "page")

    async def cancel(self, reason):
        h, item = self.h, self.current
        if not item:
            return
        self.current = None
        self.cancelled_calls.add(item.call_id)
        response = h.responses.get(item.response_id)
        if response:
            response.interrupted = response.done = True
            response.envelope.windows.clear()
            h._record_interrupted(response, reason)
            await h.emit({"type": "audio.clear", "response_ids": [response.id], "reason": reason}, "audio")
            await h._activity("output", 0, response.id)
        await h.emit({"type": "narration.stopped", "narration_id": item.id,
                      "source_call_id": item.call_id, "response_id": item.response_id, "reason": reason}, "page")
        if reason == "page_cancelled" and h.provider:
            await h.set_state("listening")
