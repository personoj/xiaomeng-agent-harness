"""Read-only session metadata: browsing must never recover or rewrite stored calls."""
import json
import re


def read_pairing(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if re.fullmatch(r"[a-f0-9]{32}", value.get("session_id", "")) and isinstance(value.get("token"), str):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def list_sessions(cfg, live):
    pairing = read_pairing(cfg.device_pairing_file)
    paired = pairing["session_id"] if pairing else None
    records = {}
    for path in cfg.data_dir.glob("*.json"):
        if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("id") != path.stem:
                continue
            records[path.stem] = (value, path.stat().st_mtime)
        except (OSError, ValueError):
            continue
    for sid, harness in live.items():
        records[sid] = (harness.store.data, records.get(sid, ({}, 0))[1])
    if paired and paired not in records:
        records[paired] = ({}, 0)
    result = []
    for sid, (value, modified) in records.items():
        harness = live.get(sid)
        history = value.get("history", [])
        created = value.get("created_at") or (history[0].get("at", modified) if history else modified)
        result.append({
            "session_id": sid, "name": str(value.get("name", ""))[:60],
            "created_at": created, "updated_at": value.get("updated_at", modified),
            "state": harness.state if harness else "disconnected",
            "page_connected": bool(harness and "page" in harness.channels),
            "audio_connected": bool(harness and "audio" in harness.channels),
            "is_device": sid == paired, "history_entries": len(history),
        })
    result.sort(key=lambda item: (item["is_device"], item["updated_at"]), reverse=True)
    return {"sessions": result, "device_session_id": paired, "device_available": bool(pairing)}
