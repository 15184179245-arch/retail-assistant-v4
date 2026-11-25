"""Transport capacity, not a character quota. Never truncate user input."""
import json
from fastapi import HTTPException

MAX_INPUT_JSON_BYTES = 131072  # Existing Pi Provider wire body ceiling.

def check_input_capacity(text):
    if len(json.dumps(text, ensure_ascii=False).encode('utf-8')) > MAX_INPUT_JSON_BYTES:
        raise HTTPException(422, 'assistant_input_capacity_exceeded')
