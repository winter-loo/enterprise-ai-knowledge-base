from __future__ import annotations

import os
from typing import cast

import httpx

from shared.openai_responses import build_response_input, build_response_request, response_answer_text

CONVERSATION_SUMMARY_INSTRUCTIONS = (
    "请把对话历史压缩为一段简洁摘要。保留：用户的身份/目标/偏好、已确认的事实与约束、"
    "尚未解决的问题与待办事项。忽略寒暄和已解决的琐碎细节。保持原文语言，不要翻译。只输出摘要。"
)

# 压缩后的摘要允许占用的最大输出 token。它代表压缩点之前的全部对话记忆。
SUMMARY_MAX_OUTPUT_TOKENS = 4000


def update_summary(previous_summary: str, new_messages: list[dict[str, str]]) -> str | None:
    """把 new_messages 并入 previous_summary, 返回压缩后的摘要。

    无 LLM 配置或调用失败时返回 None, 调用方据此保留原文并稍后重试, 而不是把
    未摘要的内容标记为已摘要。
    """
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        return None
    transcript = "\n".join(f"[{message['role']}] {message['content']}" for message in new_messages)
    prompt = f"现有摘要：\n{previous_summary or '（无）'}\n\n对话内容：\n{transcript}\n\n请压缩为一段更新后的摘要。"
    try:
        with httpx.Client(timeout=300) as client:
            response = client.post(
                f"{base_url}/responses",
                headers={"Authorization": f"Bearer {api_key}"},
                json=build_response_request(
                    model,
                    CONVERSATION_SUMMARY_INSTRUCTIONS,
                    build_response_input([], prompt, 0),
                    max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
                ),
            )
            response.raise_for_status()
            return response_answer_text(cast(object, response.json()))
    except (httpx.HTTPError, ValueError):
        return None
