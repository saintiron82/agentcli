"""Named internal request plan shared by all client execution modes."""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING
from .types import Conversation, Message

if TYPE_CHECKING:
    from .providers.base import LLMProvider


@dataclass(frozen=True)
class CallPlan:
    provider: str
    provider_obj: LLMProvider | None
    conv: Conversation
    messages: list[Message]
    fallback_messages: list[Message]
    session_id: str
    model: str
    created_conversation: bool
    resolved_alias: str
    alias_to_set: str
    system_prompt_hash: str
    force_new_session: bool
