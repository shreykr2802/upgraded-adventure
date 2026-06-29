"""
api/events.py
─────────────
Server-Sent Events (SSE) helpers.

Long-running operations (analyze, setup, migrate) stream progress to the UI
as an SSE stream. This module defines:
  - the event shape (a small typed dict)
  - sse_format() to serialise one event to the SSE wire format
  - StreamingResponse media type / headers helpers

Event shape (JSON in the `data:` field):
  {"type": "stage"|"progress"|"done"|"error",
   "stage": "<machine stage name>",
   "message": "<human message>",
   "current": <int|null>, "total": <int|null>,
   "payload": <any|null>}

The UI listens with EventSource and switches on `type`:
  stage    — a new phase began (e.g. "scanning", "resolving")
  progress — incremental update (current/total for a progress bar)
  done     — terminal success; `payload` carries the result summary
  error    — terminal failure; `message` carries the reason
"""

from __future__ import annotations

import json
from typing import Any, Iterator


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",   # disable proxy buffering so events flush immediately
}
SSE_MEDIA_TYPE = "text/event-stream"


def event(
    type_: str,
    message: str = "",
    stage: str | None = None,
    current: int | None = None,
    total: int | None = None,
    payload: Any | None = None,
) -> dict:
    """Build one progress event dict."""
    return {
        "type": type_,
        "stage": stage,
        "message": message,
        "current": current,
        "total": total,
        "payload": payload,
    }


def sse_format(evt: dict) -> str:
    """Serialise an event dict to the SSE wire format."""
    return f"data: {json.dumps(evt)}\n\n"


def stage_event(stage: str, message: str = "") -> str:
    return sse_format(event("stage", message=message or stage, stage=stage))


def progress_event(stage: str, current: int, total: int, message: str = "") -> str:
    return sse_format(event("progress", message=message, stage=stage,
                            current=current, total=total))


def done_event(payload: Any, message: str = "complete") -> str:
    return sse_format(event("done", message=message, payload=payload))


def error_event(message: str) -> str:
    return sse_format(event("error", message=message))
