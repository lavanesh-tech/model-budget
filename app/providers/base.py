from dataclasses import dataclass
from typing import Protocol


class ProviderError(Exception):
    """Raised when a provider fails to complete an otherwise-valid request."""


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    prompt: str
    max_tokens: int

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be blank")
        if not self.prompt.strip():
            raise ValueError("prompt must not be blank")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")


@dataclass(frozen=True)
class CompletionResult:
    provider: str
    model: str
    completion_text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class ProviderAdapter(Protocol):
    name: str

    async def complete(
        self,
        request: CompletionRequest,
    ) -> CompletionResult:
        """Execute a completion request against this provider.

        Raises ProviderError if the provider cannot complete an otherwise
        valid request.
        """
        ...