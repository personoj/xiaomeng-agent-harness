"""Ephemeral PCM energy envelopes. Never persist audio or add telemetry to model history."""
from array import array
from collections import deque
from dataclasses import dataclass, field
import math
import sys


def energy(pcm: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return sum(x * x for x in samples) / (len(samples) * 32768 ** 2) if samples else 0.0


def level(mean_square: float) -> float:
    return round(min(1.0, math.sqrt(mean_square) * 4), 4)


@dataclass
class PlaybackEnvelope:
    # 150 seconds of pending 40-ms windows; bytes are discarded immediately.
    windows: deque = field(default_factory=lambda: deque(maxlen=3750))
    samples: int = 0

    def append(self, pcm: bytes):
        for start in range(0, len(pcm), 1920):
            chunk = pcm[start:start + 1920]
            end = self.samples + len(chunk) // 2
            self.windows.append((self.samples, end, energy(chunk)))
            self.samples = end

    def played(self, previous: int, current: int) -> float:
        total = count = 0
        for start, end, value in self.windows:
            overlap = max(0, min(current, end) - max(previous, start))
            total += value * overlap
            count += overlap
            if end >= current:
                break
        while self.windows and self.windows[0][1] <= current:
            self.windows.popleft()
        return level(total / count) if count else 0.0
