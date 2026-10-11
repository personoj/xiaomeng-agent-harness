"""Associate native speech items with turns; ASR is evidence, not a second intent model."""
import asyncio
from dataclasses import dataclass, field


@dataclass
class SpeechInput:
    turn: int
    item_id: str | None = None
    text: str = ""
    invalid: bool = False
    completed: asyncio.Event = field(default_factory=asyncio.Event)

    def finish(self, text="", invalid=False):
        self.text = text.strip() if isinstance(text, str) else ""
        self.invalid = self.invalid or invalid
        self.completed.set()


class SpeechInputs:
    def __init__(self):
        self.turns = {}
        self.items = {}

    def start(self, turn, item_id=None):
        for old in self.turns.values():
            if not old.completed.is_set():
                old.finish(invalid=True)
        value = SpeechInput(turn, item_id)
        self.turns[turn] = value
        if item_id:
            self.items[item_id] = value
        while len(self.turns) > 64:
            old = self.turns.pop(next(iter(self.turns)))
            self.items.pop(old.item_id, None)
        return value

    def lookup(self, event, current_turn):
        item_id = event.get("item_id")
        if item_id in self.items:
            return self.items[item_id]
        current = self.turns.get(current_turn)
        if current and (not item_id or not current.item_id):
            if item_id:
                current.item_id = item_id
                self.items[item_id] = current
            return current
        return None
