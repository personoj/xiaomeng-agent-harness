"""Local durable facts. Provider transcripts are a projection, not the source of truth."""
from pathlib import Path
import json
import re
import time
import uuid


class SessionStore:
    def __init__(self, directory: Path, session_id: str | None = None):
        self.id = session_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", self.id):
            raise ValueError("invalid_session_id")
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / (self.id + ".json")
        now = time.time()
        self.data = {"version": 1, "id": self.id, "name": "", "created_at": now, "updated_at": now, "history": [], "calls": {}, "turn": 0}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            # A process crash cannot prove whether a dispatched external write took effect.
            for call in self.data["calls"].values():
                if call["status"] in ("running", "dispatched"):
                    call["status"] = "unknown"
                    call["result"] = {"error": "process_restarted", "outcome": "unknown"}
            self.save()

    def save(self):
        self.data["updated_at"] = time.time()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def history(self, kind: str, text: str, **fields):
        self.data["history"].append({"kind": kind, "text": text, "at": time.time(), **fields})
        self.save()

    def projection(self, budget: int) -> str:
        # Whole entries only, newest first for selection; partial answers never become heard text.
        selected, used = [], 0
        for row in reversed(self.data["history"]):
            item = {k: v for k, v in row.items() if k not in ("at",)}
            encoded = json.dumps(item, ensure_ascii=False)
            if used + len(encoded) > budget:
                break
            selected.append(item)
            used += len(encoded)
        selected.reverse()
        return json.dumps(selected, ensure_ascii=False)
