"""Offline injected-delay benchmark; no model, network, browser or device calls.

Run this same script with --repo pointing to the untouched and modified checkouts.
It measures Harness receipt of a real-shaped final playback report to the request
for completion speech, not model inference or acoustic playback latency.
"""
import argparse
import asyncio
import json
import platform
from pathlib import Path
import statistics
import sys
import tempfile
import time


class OfflineProvider:
    def __init__(self, cfg):
        self.requested = asyncio.Event()
        self.requested_at = None
        self.queue = asyncio.Queue()

    async def connect(self, instructions, tools):
        pass

    async def send(self, event):
        if event['type'] == 'response.create':
            self.requested_at = time.perf_counter()
            self.requested.set()

    async def events(self):
        while True:
            yield await self.queue.get()

    async def close(self):
        pass


async def measure(delay_ms):
    from xiaoyun.config import Settings
    from xiaoyun.harness import Harness, Response
    from xiaoyun.memory import SessionStore
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        h = Harness(Settings(data_dir=path), SessionStore(path), OfflineProvider)
        async def sink(event):
            pass
        conn = await h.attach('duplex', sink)
        await h.handle(conn, {'type': 'context.update', 'page_id': 'synthetic', 'context': {}})
        await h.start()
        try:
            h.turn_started_at = time.monotonic() - 1
            h.store.data['calls']['call'] = {
                'status': 'succeeded', 'page_id': 'synthetic', 'result': {'task_id': 'task'},
            }
            h.responses['response'] = Response('response', h.turn, h.revision,
                                               done=True, generated_samples=2400)
            await h.handle(conn, {'type': 'task.update', 'task_id': 'task',
                                  'source_call_id': 'call', 'status': 'succeeded', 'result': {}})
            for _ in range(10):
                await asyncio.sleep(0)
            assert not h.provider.requested.is_set()
            await asyncio.sleep(delay_ms / 1000)
            received_at = time.perf_counter()
            await h.handle(conn, {'type': 'playback.progress', 'response_id': 'response',
                                  'played_samples': 2400, 'drained': True})
            await asyncio.wait_for(h.provider.requested.wait(), 1)
            return round((h.provider.requested_at - received_at) * 1000, 3)
        finally:
            await h.stop()


async def main(args):
    trials = [{'injected_delay_ms': delay, 'receipt_to_request_ms': await measure(delay)}
              for _ in range(args.repeats) for delay in (5, 20, 50, 90)]
    values = sorted(row['receipt_to_request_ms'] for row in trials)
    def percentile(p):
        return values[min(len(values)-1, int((len(values)-1)*p+.5))]
    print(json.dumps({
        'scope': 'synthetic offline Harness scheduler only; not device/model/E2E latency',
        'repo': str(args.repo.resolve()), 'python': platform.python_version(),
        'platform': platform.platform(), 'samples': len(values),
        'p50_ms': statistics.median(values), 'p95_ms': percentile(.95),
        'p99_ms': percentile(.99), 'max_ms': max(values), 'trials': trials,
    }, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    sys.path.insert(0, str(args.repo.resolve()))
    asyncio.run(main(args))
