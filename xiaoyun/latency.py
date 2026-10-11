"""Bounded, monotonic stage timings. Receipts are not remote/acoustic timestamps."""
import time


class TurnTimings:
    STAGES = {
        'input.committed': 'input_commit_sent',
        'transcript.user': 'final_transcript_ready',
        'tool.request': 'tool_dispatch_started',
        'tool.completed': 'tool_result_recorded',
        'task.completed': 'task_result_reported',
        'task.announcing': 'task_announcement_requested',
        'response.started': 'response_created',
        'response.generated': 'audio_generation_finished',
        'playback.observed': 'first_playback_progress_received',
        'playback.completed': 'playback_drain_received',
    }

    def __init__(self):
        self.origins = {}

    def start(self, turn, origin, at):
        self.origins[turn] = (origin, at)
        while len(self.origins) > 64:
            self.origins.pop(next(iter(self.origins)))

    def stamp(self, event):
        stage = self.STAGES.get(event['type'])
        if event['type'] == 'metrics' and 'first_audio_ms' in event:
            stage = 'first_generated_audio_received'
        if event['type'] == 'audio.delta' and event.get('seq') == 1:
            stage = 'first_audio_dispatch_started'
        turn = event.get('source_turn', event['turn'])
        if stage and event.get('task_sources'):
            sources = []
            for source in event['task_sources']:
                source_turn = source['source_turn']
                origin = self.origins.get(source_turn)
                sources.append({**source, 'trace_id': f"{event['session_id']}:{source_turn}",
                                **({'clock': 'harness_monotonic', 'origin': origin[0], 'stage': stage,
                                    'elapsed_ms': round((time.monotonic() - origin[1]) * 1000, 3)}
                                   if origin else {})})
            event = {**event, 'task_sources': sources}
        origin = self.origins.get(turn)
        if not stage or origin is None:
            return event
        name, started_at = origin
        if stage == 'final_transcript_ready' and name == 'input.text_received':
            stage = 'user_text_ready'
        return {**event, 'latency': {
            'trace_id': f"{event['session_id']}:{turn}", 'turn': turn,
            'clock': 'harness_monotonic', 'origin': name, 'stage': stage,
            'elapsed_ms': round((time.monotonic() - started_at) * 1000, 3),
        }}
