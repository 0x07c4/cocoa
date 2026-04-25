from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderRequest:
    thread_id: str
    turn_id: str
    prompt: str
    cwd: str


@dataclass(frozen=True)
class ProviderResponse:
    message: str
    summary: str | None = None


class ProviderAdapter(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError


class StubProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        lines = [
            "Provider is not configured yet.",
            "This turn has been recorded by cocoa's runtime.",
            "Next step: connect an OpenAI-compatible provider adapter.",
        ]
        return ProviderResponse(
            message="\n".join(lines),
            summary=f"stub response for: {request.prompt[:80]}",
        )

