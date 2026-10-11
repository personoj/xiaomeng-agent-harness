# Latency and cancellation instrumentation

`input.commit` timing begins when Harness receives the event, before its session-lock wait. Selected lifecycle events have an additive `latency` object with a `session_id:turn` trace ID, `clock=harness_monotonic`, named origin/stage and elapsed milliseconds. This is a host-local measurement; never subtract ESP uptime or browser `performance.now()` from it.

Stage names deliberately distinguish `first_generated_audio_received`, `first_audio_dispatch_started`, `first_playback_progress_received` and `playback_drain_received`. The first positive sample report is evidence of playback, but its host receipt includes reporting/network delay. `tool_result_recorded` may be asynchronous acceptance; `task_result_reported` is the separately validated terminal task report. State acceptance is not GPU presentation.

Task result announcements retain `task_sources` through response generation and playback, including task/call IDs, original turn and source trace. If an announcement occurs in a later user turn, top-level latency belongs to that execution turn and each task source carries its own original-input latency. Origin timestamps are bounded to 64 turns; old IDs remain available without invented elapsed values. Legacy `metrics.first_audio_ms` is generated-audio latency and is labeled with its origin. For result announcements it starts at the announcement request, not the original physical task request.

The completion-notification worker is event-driven. It retains the existing 500 ms quiet guard, waits for explicit playback drain, gives new speech priority, deduplicates task facts and does not start a stopped model session.

Interruption treats generated audio as outstanding until an explicit drain receipt, even after the full sample count is reported. A client may clear playback before receiving a delayed `audio.end`, so cancelled responses must not require a future drain to release task notifications. Cancellation does not mark playback complete, record the answer as heard, or stop accepted page jobs.

The page SDK's `execution.isCurrent()` checks revision, user turn, transport generation, session-stop generation and response cancellation. Host actions must recheck immediately before any mutation after awaited reads/decisions. Deploy this SDK together with the workshop controller's post-await checks. A voice interruption does not stop already-submitted mechanical work; late outcomes remain durable, without overwriting newer context or being sent through an unrelated replacement socket.

Offline checks (from this repository):

```sh
../.venv/bin/python -m pytest -q
node --test tests/browser.test.mjs
../.venv/bin/python scripts/benchmark_task_notification.py --repo .
../.venv/bin/python scripts/benchmark_task_notification.py --repo ../baselines/xiaoyun-realtime
```

The benchmark injects delays into a fake-provider, synthetic-session workload. It measures final playback-report receipt to requesting completion speech. It does not call a model, access a device or measure online p95, first visible frame, mechanical duration or acoustic latency.
