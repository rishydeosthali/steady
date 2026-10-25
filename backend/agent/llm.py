"""Thin OpenAI wrapper. OpenAI is the only language model.

Errors are reduced to a type and status code: OpenAI auth errors can echo a
fragment of the key in their message, so messages are never logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI, OpenAIError

log = logging.getLogger(__name__)


class LLMError(Exception):
    """The language model call failed. Message is safe to log."""


class LLM:
    def __init__(self, api_key: str, model: str, *, client: OpenAI | None = None):
        self.model = model
        self._client = client or OpenAI(api_key=api_key, timeout=20.0, max_retries=2)

    def json_completion(self, system: str, user: str, schema_name: str,
                        schema: dict[str, Any]) -> dict[str, Any]:
        """One chat completion constrained to a JSON schema.

        ``system`` is Steady's fixed instructions. ``user`` is the only place
        untrusted text goes.
        """
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
            )
            return json.loads(resp.choices[0].message.content or "")
        except OpenAIError as exc:
            status = getattr(exc, "status_code", None)
            raise LLMError(f"OpenAI call failed: {type(exc).__name__} status={status}") from None
        except (ValueError, IndexError, AttributeError):
            raise LLMError("OpenAI returned an unparseable response") from None
