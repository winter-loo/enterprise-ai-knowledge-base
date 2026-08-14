from __future__ import annotations

from typing import Literal, TypedDict, cast

ResponseRole = Literal["user", "assistant"]


class ResponseInputMessage(TypedDict):
    role: ResponseRole
    content: str


def build_response_input(history: list[dict[str, str]], prompt: str, history_limit: int) -> list[ResponseInputMessage]:
    """Convert stored chat messages into OpenAI Responses API input items."""
    messages: list[ResponseInputMessage] = []
    selected_history = history[-history_limit:] if history_limit > 0 else []
    for message in selected_history:
        role = message.get("role")
        content = message.get("content", "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        messages.append({"role": cast(ResponseRole, role), "content": content})
    messages.append({"role": "user", "content": prompt})
    return messages


def build_response_request(
    model: str,
    instructions: str,
    input_messages: list[ResponseInputMessage],
    *,
    stream: bool = False,
    max_output_tokens: int | None = None,
) -> dict[str, object]:
    request: dict[str, object] = {
        "model": model,
        "instructions": instructions,
        "input": input_messages,
        "store": False,
    }
    if stream:
        request["stream"] = True
    if max_output_tokens is not None:
        request["max_output_tokens"] = max_output_tokens
    return request


def response_answer_text(payload: object) -> str:
    """Extract visible answer text, including refusals, from a completed response."""
    response = _mapping(payload)
    if response is None or response.get("status") != "completed":
        raise ValueError("LLM response did not complete")

    texts: list[str] = []
    for item_value in _items(response.get("output")):
        item = _mapping(item_value)
        if item is None:
            continue
        for part_value in _items(item.get("content")):
            part = _mapping(part_value)
            if part is None:
                continue
            part_type = part.get("type")
            text = part.get("text") if part_type == "output_text" else part.get("refusal") if part_type == "refusal" else None
            if isinstance(text, str):
                texts.append(text)

    answer = "".join(texts).strip()
    if not answer:
        raise ValueError("LLM response returned no output text")
    return answer


def stream_delta(payload: object) -> str:
    event = _mapping(payload)
    if event is None or event.get("type") not in {"response.output_text.delta", "response.refusal.delta"}:
        return ""
    delta = event.get("delta")
    return delta if isinstance(delta, str) else ""


def stream_completed(payload: object) -> bool:
    event = _mapping(payload)
    return event is not None and event.get("type") == "response.completed"


def stream_error(payload: object) -> str | None:
    event = _mapping(payload)
    if event is None:
        return None

    event_type = event.get("type")
    if event_type == "error":
        message = event.get("message")
        return message if isinstance(message, str) and message else "Upstream LLM returned an error"

    nested_response = _mapping(event.get("response"))
    if event_type == "response.failed":
        error = _mapping(nested_response.get("error")) if nested_response is not None else None
        message = error.get("message") if error is not None else None
        return message if isinstance(message, str) and message else "Upstream LLM response failed"
    if event_type == "response.incomplete":
        details = _mapping(nested_response.get("incomplete_details")) if nested_response is not None else None
        reason = details.get("reason") if details is not None else None
        return f"Upstream LLM response was incomplete: {reason}" if isinstance(reason, str) and reason else "Upstream LLM response was incomplete"

    error = _mapping(event.get("error"))
    message = error.get("message") if error is not None else None
    return message if isinstance(message, str) and message else None


def _mapping(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _items(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []
