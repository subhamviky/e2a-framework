"""Canonical model types (v1.6.0, design note MP-02, proposal P5).

One vendor-neutral shape for a conversation and a model's answer, so that every provider
adapter maps to the same thing instead of inventing its own. They are plain dataclasses that
read and write the dicts E2A already passes around, so dict-based code keeps working:

    Message.from_dict(m).to_dict() == m          (for a canonical dict m)
    ModelResult(...).to_raw()                    what _dispatch_upstream_call may return

The wire mapping of each provider (Anthropic Messages, OpenAI Responses / Chat Completions,
Gemini, ...) is NOT here: it is vendor knowledge and lives in the provider layer. Core holds
only the shape every mapping targets.

Canonical message dict
    {"role": "system" | "user" | "assistant" | "tool",
     "content": str | [{"type": "text", "text"} | {"type": "image", "media_type", "data" | "url"}
                       | {"type": "document", "media_type", "data" | "file_id"}],
     "tool_calls": [{"id", "name", "arguments": dict}],    # assistant only
     "tool_call_id": str}                                   # tool only
"""
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

__all__ = ['StopReason', 'ToolCall', 'ToolDeclaration', 'Usage', 'Message', 'ModelResult', 'parse_arguments']


class StopReason(str, Enum):
    END_TURN = 'end_turn'
    TOOL_USE = 'tool_use'
    MAX_TOKENS = 'max_tokens'
    STOP_SEQUENCE = 'stop_sequence'
    REFUSAL = 'refusal'
    PAUSE = 'pause'
    ERROR = 'error'


def parse_arguments(value: Any) -> Dict[str, Any]:
    """Tool arguments are always a parsed dict. Providers send a dict or a JSON string: the
    string is parsed, never string-matched. Unparseable input is kept under '_raw'."""
    if isinstance(value, dict):
        return value
    if value in (None, ''):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {'_raw': value}
    return parsed if isinstance(parsed, dict) else {'value': parsed}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.arguments = parse_arguments(self.arguments)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'ToolCall':
        return cls(id=str(d.get('id') or ''), name=str(d['name']), arguments=d.get('arguments'))

    def to_dict(self) -> Dict[str, Any]:
        return {'id': self.id, 'name': self.name, 'arguments': dict(self.arguments)}


@dataclass
class ToolDeclaration:
    name: str
    description: str = ''
    parameters: Dict[str, Any] = field(default_factory=lambda: {'type': 'object', 'properties': {}})
    strict: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {'name': self.name, 'description': self.description, 'parameters': self.parameters,
                **({'strict': True} if self.strict else {})}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> 'Usage':
        d = d or {}
        return cls(**{k: int(d.get(k) or 0) for k in cls.__dataclass_fields__})

    def to_dict(self) -> Dict[str, int]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Message:
    role: str
    content: Union[str, List[Dict[str, Any]]] = ''
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None

    def __post_init__(self):
        if self.role not in ('system', 'user', 'assistant', 'tool'):
            raise ValueError(f"message role must be system, user, assistant or tool, not {self.role!r}")
        self.tool_calls = [c if isinstance(c, ToolCall) else ToolCall.from_dict(c) for c in self.tool_calls]

    @property
    def text(self) -> str:
        """The text of the message, whatever the content shape."""
        if isinstance(self.content, str):
            return self.content
        return ''.join(str(p.get('text', '')) for p in self.content if isinstance(p, dict) and p.get('type') == 'text')

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'Message':
        return cls(role=d['role'], content=d.get('content', ''), tool_calls=list(d.get('tool_calls') or []),
                   tool_call_id=d.get('tool_call_id'))

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {'role': self.role, 'content': self.content}
        if self.tool_calls:
            out['tool_calls'] = [c.to_dict() for c in self.tool_calls]
        if self.tool_call_id is not None:
            out['tool_call_id'] = self.tool_call_id
        return out


@dataclass
class ModelResult:
    """What a provider transport returns from _dispatch_upstream_call (or the equivalent
    dict from to_raw(); the base accepts both)."""
    text: str = ''
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: Optional[StopReason] = None
    model: Optional[str] = None
    provider_request_id: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.tool_calls = [c if isinstance(c, ToolCall) else ToolCall.from_dict(c) for c in self.tool_calls]
        if isinstance(self.usage, dict):
            self.usage = Usage.from_dict(self.usage)
        if self.stop_reason is not None and not isinstance(self.stop_reason, StopReason):
            self.stop_reason = StopReason(self.stop_reason)

    def to_raw(self) -> Dict[str, Any]:
        """The dict shape BaseLLMAgentService reads."""
        raw: Dict[str, Any] = {'text': self.text, 'metadata': dict(self.raw_metadata), 'usage': self.usage.to_dict()}
        raw['metadata'].setdefault('tokens_used', self.usage.total_tokens)
        if self.tool_calls:
            raw['tool_calls'] = [c.to_dict() for c in self.tool_calls]
        if self.stop_reason is not None:
            raw['stop_reason'] = self.stop_reason.value
        if self.provider_request_id:
            raw['provider_request_id'] = self.provider_request_id
        if self.model:
            raw['metadata'].setdefault('model', self.model)
        return raw
