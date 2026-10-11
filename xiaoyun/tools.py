from dataclasses import dataclass
from typing import Awaitable, Callable
import re

from jsonschema import Draft202012Validator


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    side_effect: bool = False
    handler: Callable[[dict], Awaitable[dict]] | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}", self.name):
            raise ValueError("invalid_tool_name")
        if self.parameters.get("type") != "object":
            raise ValueError("tool_parameters_must_be_object")
        Draft202012Validator.check_schema(self.parameters)
        self.validator = Draft202012Validator(self.parameters)

    def definition(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}

    def validate(self, arguments):
        self.validator.validate(arguments)
