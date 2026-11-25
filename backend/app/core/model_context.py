"""Model-window policy shared by HTTP, conversations, workers and providers.

Window sizes are deployment metadata, not spend authorization. Token estimates
are admission heuristics, not billing counts; providers remain authoritative.
Unknown deployments retain the legacy per-call capacity until configured.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit


# Transport/UI ceiling, independent of the selected model's token window.
MAX_MESSAGE_CHARS = 4_200_000
LEGACY_INPUT_TOKENS = 32_768
DEFAULT_OUTPUT_RESERVE = 16_384
MESSAGE_OVERHEAD = 16
PROTOCOL_RESERVE = 64

# Official API model pages / DeepSeek Codex integration, checked 2026-11-01.
# Codex's own account catalog is separate: its advertised max is 872,000.
KNOWN_CONTEXT_WINDOWS = {
    "gpt-6.1-sol": 1_050_000,
    "deepseek-flash": 1_048_576,
    "deepseek-v4-pro": 1_048_576,
}


class ModelContextError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def context_window(model: str | None = None, *,
                   environ: Mapping[str, str] | None = None,
                   base_url: str | None = None) -> int | None:
    env = os.environ if environ is None else environ
    raw = env.get("AGENT_PROVIDER_CONTEXT_WINDOW", "")
    if raw:
        try:
            value = int(raw)
        except (ValueError, TypeError):
            raise ModelContextError("agent_context_configuration_invalid") from None
        if not 128 <= value <= 16_777_216:
            raise ModelContextError("agent_context_configuration_invalid")
        return value
    selected_model = model or env.get("AGENT_PROVIDER_MODEL", "")
    window = KNOWN_CONTEXT_WINDOWS.get(selected_model)
    deployment = base_url if base_url is not None else env.get("AGENT_PROVIDER_BASE_URL", "")
    if window is not None and deployment:
        expected_host = ("api.openai.com" if selected_model == "gpt-6.1-sol"
                         else "api.deepseek.com")
        try:
            parts = urlsplit(deployment)
            official = (parts.scheme == "https" and parts.hostname == expected_host
                        and parts.port in (None, 443) and parts.username is None
                        and parts.password is None)
        except (TypeError, ValueError):
            official = False
        if not official:
            return None
    return window


def estimate_tokens(text: str) -> int:
    """CJK/Latin admission heuristic, NOT a tokenizer or a billing oracle."""
    cjk = sum(1 for ch in text if "\u2e80" <= ch <= "\u9fff"
              or "\uff00" <= ch <= "\uffef")
    other_unicode = sum(1 for ch in text if ord(ch) > 127
                        and not ("\u2e80" <= ch <= "\u9fff"
                                 or "\uff00" <= ch <= "\uffef"))
    latin = len(text) - cjk - other_unicode
    return cjk * 2 + other_unicode * 4 + (latin + 3) // 4


def input_capacity(model: str | None = None, *, output_tokens: int = DEFAULT_OUTPUT_RESERVE,
                   remaining_input: int | None = None) -> int:
    window = context_window(model)
    capacity = LEGACY_INPUT_TOKENS if window is None else window - output_tokens
    if remaining_input is not None:
        capacity = min(capacity, remaining_input)
    if capacity <= PROTOCOL_RESERVE:
        raise ModelContextError("agent_context_window_exceeded")
    return capacity


def run_input_budget(model: str, *, model_calls: int, output_tokens: int,
                     explicit_limit: int | None = None) -> int:
    if explicit_limit is not None:
        if explicit_limit <= 0:
            raise ModelContextError("agent_context_configuration_invalid")
        return explicit_limit
    capacity = input_capacity(model, output_tokens=output_tokens)
    # A context window bounds one request; a Run accounts for all requests.
    # Repeated system/tool/history input is billed again on each model call,
    # including on deployments whose exact context window is not known.
    # This does not enlarge input_capacity() or a campaign's per-call limit.
    return capacity * model_calls


def fit_prompt(render: Callable[[dict | None], str], context: dict | None,
               max_input_tokens: int) -> str:
    """Fit the complete prompt by dropping whole oldest history messages.

    Current input, tools and instructions are mandatory. Never truncate their
    text, or modify stored/frozen context. Overlong mandatory input fails before
    a cost reservation or provider dispatch.
    The renderer must be deterministic for the same context.
    """
    def fits(value: str) -> bool:
        return estimate_tokens(value) + PROTOCOL_RESERVE <= max_input_tokens

    prompt = render(context)
    if fits(prompt):
        return prompt
    messages = list((context or {}).get("messages") or [])
    empty = {**(context or {}), "messages": [], "truncated": True}
    prompt = render(empty)
    if not fits(prompt):
        raise ModelContextError("agent_context_window_exceeded")
    # The suffix of newest complete messages is monotonic in prompt size.
    # Keep the exact fitting rendering for the current upper bound.
    low, high = 0, len(messages)
    while low < high:
        mid = (low + high) // 2
        candidate = render({**empty, "messages": messages[mid:]})
        if fits(candidate):
            high = mid
            prompt = candidate
        else:
            low = mid + 1
        del candidate
    return prompt
