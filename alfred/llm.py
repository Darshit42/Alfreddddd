"""The only module that talks to a model provider.

The loop depends on a tiny interface: complete(system, messages, tools)
returns an object with .content (blocks with .type) and .stop_reason. Tests
plug in a scripted fake; production uses Claude.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import anthropic

DEFAULT_MODEL = "claude-opus-5-5"
# USD per million tokens: (input, output, cache read, cache write). Used for the run's cost cap and metrics.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20, 5.0), "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
          "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25)}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, u) -> None:
        self.calls += 1
        self.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0

    def cost(self, model: str) -> float:
        """Estimated spend in USD (0 for a model with no listed price)."""
        i, o, r, w = PRICES.get(model, (0, 0, 0, 0))
        return (self.input_tokens * i + self.output_tokens * o + self.cache_read_tokens * r
                + self.cache_write_tokens * w) / 1_000_000


@dataclass
class ClaudeLLM:
    model: str = field(default_factory=lambda: os.environ.get("ALFRED_MODEL", DEFAULT_MODEL))
    effort: str = field(default_factory=lambda: os.environ.get("ALFRED_EFFORT", "high"))
    max_tokens: int = 16_000
    # If a safety classifier declines a turn, let the API rerun it on its recommended
    # fallback model instead of failing the whole task. ALFRED_FALLBACKS=off disables.
    fallbacks: bool = field(default_factory=lambda: os.environ.get("ALFRED_FALLBACKS", "on") != "off")
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self) -> None:
        # Reads ANTHROPIC_API_KEY. The SDK already retries 429 / 5xx / connection errors with backoff.
        self.client = anthropic.Anthropic(max_retries=4)

    def cost(self) -> float:
        return self.usage.cost(self.model)

    def complete(self, system: str, messages: list, tools: list):
        kwargs: dict = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            # The conversation only ever grows, so everything before the newest turn is a cache hit.
            cache_control={"type": "ephemeral"},
        )
        if "haiku" not in self.model:
            # "summarized" returns readable reasoning, which is what makes the trace explainable.
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
            kwargs["output_config"] = {"effort": self.effort}
        response = self._create(kwargs)
        self.usage.add(response.usage)
        return response

    def _create(self, kwargs: dict):
        if self.fallbacks:
            try:
                return self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise
                self.fallbacks = False  # not available for this model or account; carry on without it
        return self.client.messages.create(**kwargs)
