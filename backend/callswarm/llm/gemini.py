"""GeminiProvider on the official unified Google GenAI SDK (``google-genai``).

Auth: ``GEMINI_API_KEY``, or Vertex AI with Application Default Credentials
when ``GOOGLE_GENAI_USE_VERTEXAI=true``. The model name comes from settings;
nothing here hardcodes one.

Structured output: every call requests JSON conforming to the Pydantic schema
and validates the result. One retry on validation failure with the validator
error appended; then :class:`AgentOutputInvalid`. Raw model text is never
returned, logged or stored.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from callswarm.config.settings import Settings
from callswarm.llm.prompt import build_system_instruction, build_user_content
from callswarm.llm.provider import (
    AgentOutputInvalid,
    LLMModelUnavailable,
    LLMNotConfigured,
    TModel,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2

VerificationStatus = Literal["not_configured", "unverified", "verified", "unavailable"]


class ModelVerification(BaseModel):
    status: VerificationStatus
    model: str
    detail: str | None = None


def _validation_messages(error: ValidationError) -> list[str]:
    messages: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        messages.append(f"{location}: {item.get('msg', 'invalid')}")
    return messages


def _normalize_model_name(name: str) -> str:
    return name.removeprefix("models/").removeprefix("publishers/google/models/")


class GeminiProvider:
    """LLMProvider backed by Gemini. The client is created lazily, never at import."""

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client: Any | None = client
        self.model: str = settings.gemini_model
        self.verification: ModelVerification = ModelVerification(
            status="not_configured" if not settings.llm_configured else "unverified",
            model=self.model,
        )

    # --- client -----------------------------------------------------------
    @property
    def configured(self) -> bool:
        return self._settings.llm_configured

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.configured:
            raise LLMNotConfigured("no GEMINI_API_KEY and Vertex AI is not enabled")
        from google import genai

        settings = self._settings
        if settings.google_genai_use_vertexai:
            self._client = genai.Client(
                vertexai=True,
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
            )
        else:
            assert settings.gemini_api_key is not None
            self._client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        return self._client

    # --- startup check ------------------------------------------------------
    async def verify_model(self) -> ModelVerification:
        """Confirm the configured model appears in ``client.models.list()``.

        Skipped cleanly (status ``not_configured``) when no credentials exist.
        """
        if not self.configured:
            self.verification = ModelVerification(status="not_configured", model=self.model)
            return self.verification
        wanted = _normalize_model_name(self.model)
        try:
            client = self._get_client()
            available: list[str] = []
            async for item in await client.aio.models.list():
                name = getattr(item, "name", None)
                if isinstance(name, str):
                    available.append(_normalize_model_name(name))
        except Exception as exc:  # any SDK or transport failure is a verification failure
            logger.error("Gemini model verification failed: %s", type(exc).__name__)
            self.verification = ModelVerification(
                status="unavailable",
                model=self.model,
                detail=f"listing failed: {type(exc).__name__}",
            )
            return self.verification
        if wanted in available:
            self.verification = ModelVerification(status="verified", model=self.model)
        else:
            logger.error("Configured Gemini model %r is not available", self.model)
            self.verification = ModelVerification(
                status="unavailable",
                model=self.model,
                detail="model not present in provider listing",
            )
        return self.verification

    # --- generation ---------------------------------------------------------
    async def _generate_text(
        self, system_instruction: str, content: str, schema: type[BaseModel]
    ) -> str:
        from google.genai import types

        client = self._get_client()
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
        )
        response = await client.aio.models.generate_content(
            model=self.model, contents=content, config=config
        )
        text = getattr(response, "text", None)
        return text if isinstance(text, str) else ""

    async def generate_structured(
        self, instruction: str, inputs: Mapping[str, str], schema: type[TModel]
    ) -> TModel:
        if self.verification.status == "unavailable":
            raise LLMModelUnavailable(f"model {self.model!r} failed startup verification")
        system_instruction = build_system_instruction(instruction)
        content = build_user_content(inputs)
        errors: list[str] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            raw = await self._generate_text(system_instruction, content, schema)
            try:
                return schema.model_validate_json(raw)
            except ValidationError as exc:
                errors = _validation_messages(exc)
                logger.warning(
                    "schema %s invalid on attempt %d: %d error(s)",
                    schema.__name__,
                    attempt,
                    len(errors),
                )
                system_instruction = (
                    f"{system_instruction}\n\nYour previous response did not satisfy the required "
                    f"JSON schema. Validation errors: {'; '.join(errors)}. "
                    "Return only corrected JSON that satisfies the schema."
                )
        raise AgentOutputInvalid(schema.__name__, errors, MAX_ATTEMPTS)
