import hashlib
import math

from app.providers.base import CompletionRequest, CompletionResult

_CHARS_PER_TOKEN = 4


class MockProvider:
    name = "mock"

    async def complete(
        self,
        request: CompletionRequest,
    ) -> CompletionResult:
        prompt_tokens = _mock_token_count(request.prompt)

        full_completion_text = _deterministic_completion(request.prompt)
        completion_text = _truncate_to_token_budget(
            full_completion_text,
            request.max_tokens,
        )
        completion_tokens = _mock_token_count(completion_text)

        return CompletionResult(
            provider=self.name,
            model=request.model,
            completion_text=completion_text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=0,
        )


def _mock_token_count(text: str) -> int:
    """Apply the deterministic mock rule ceil(len(text) / 4)."""
    if text == "":
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def _truncate_to_token_budget(text: str, max_tokens: int) -> str:
    max_chars = max_tokens * _CHARS_PER_TOKEN
    return text[:max_chars]


def _deterministic_completion(prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    return f"[mock completion for prompt hash {digest}]"