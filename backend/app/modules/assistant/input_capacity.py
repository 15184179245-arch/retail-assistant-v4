"""Serialized input allowance, not a complete Provider request guarantee.

The SDK adds system/tool metadata and results. Its final serialized body keeps
its independent pre-send wire check. Neither boundary truncates user input.
"""
import json
from fastapi import HTTPException
from backend.app.modules.agents.runtime_protocol import PI_PROVIDER_BYTES
from backend.app.modules.assistant.core_adapter import packed

MAX_INPUT_JSON_BYTES = PI_PROVIDER_BYTES  # Upper bound on input alone, not reserved overhead.


def check_input_capacity(text):
    if len(json.dumps(text, ensure_ascii=False).encode("utf-8")) > MAX_INPUT_JSON_BYTES:
        raise HTTPException(422, "assistant_input_capacity_exceeded")


def compose_turn_input(root_work_id, original_request, history):
    """One byte-stable composer for intake and dispatch; historical reads stay pure."""
    return ("继续处理同一事项。以下是用户原始要求和按顺序追加的要求；以最后一次补充为本轮请求。\n"
        + packed({"root_work_id": root_work_id, "original_request": original_request,
                  "followups": [event["text"] for event in history if event["kind"] == "followup"]}).decode())
