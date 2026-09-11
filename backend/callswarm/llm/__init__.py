"""LLMProvider protocol, Gemini implementation, fake provider and prompt helpers."""

from callswarm.llm.fake import FakeLLMExhausted, FakeLLMProvider
from callswarm.llm.gemini import GeminiProvider, ModelVerification
from callswarm.llm.prompt import untrusted_block
from callswarm.llm.provider import (
    AgentOutputInvalid,
    LLMError,
    LLMModelUnavailable,
    LLMNotConfigured,
    LLMProvider,
)

__all__ = [
    "AgentOutputInvalid",
    "FakeLLMExhausted",
    "FakeLLMProvider",
    "GeminiProvider",
    "LLMError",
    "LLMModelUnavailable",
    "LLMNotConfigured",
    "LLMProvider",
    "ModelVerification",
    "untrusted_block",
]
