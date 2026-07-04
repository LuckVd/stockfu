"""OpenAI 兼容 LLM 客户端(stockfu AI 顾问用)。

设计:
- 从 config.get_llm_*() 读配置(app_config 表优先，.env 回落；面板改完热生效，无需重启)
- httpx 同步 POST {base_url}/chat/completions
- json_repair 容错解析 LLM 的 JSON 输出(LLM 常吐带 ```json 代码块或缺逗号的串)
- 超时 + 有限重试(线性退避)
- 代理默认关(use_proxy=False):opencode.ai 网关直连可达,走 7890 实测 SSL 失败;确需时再 use_proxy=True

鲁棒性思路参考 references/tradingagents(json_repair + 超时 + 降级),
但实现是 stockfu 自己的,不抄其代码。
"""
from __future__ import annotations

import json
import time
from typing import Optional

import httpx

from stockfu.config import get_llm_api_key, get_llm_base_url, get_llm_model, get_overseas_proxy


class LLMError(RuntimeError):
    """LLM 调用或解析失败。"""


def chat(
    messages: list[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    timeout: float = 60.0,
    retries: int = 2,
    use_proxy: bool = False,
) -> str:
    """调用 chat completions,返回 assistant 文本。失败线性退避重试,最终抛 LLMError。"""
    if not get_llm_api_key() or not get_llm_base_url():
        raise LLMError("LLM 未配置:请在 .env 设置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")

    # OpenAI 兼容惯例:base 含 /v1(如 https://api.openai.com/v1);用户若只给到根,
    # 自动补 /v1。opencode.ai 等中转 base 不带 /v1,需补。
    url = get_llm_base_url().rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    url += "/chat/completions"
    headers = {
        "Authorization": f"Bearer {get_llm_api_key()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or get_llm_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # 代理默认关:opencode.ai 网关直连可达,走 PROXY_URL(7890)实测 SSL 失败;确需时 use_proxy=True
    client_kwargs: dict = {"timeout": timeout}
    proxy = get_overseas_proxy() if use_proxy else None
    if proxy:
        client_kwargs["proxy"] = proxy

    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(**client_kwargs) as c:
                r = c.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"LLM 调用失败(重试 {retries + 1} 次):{type(last_exc).__name__}: {last_exc}")


def chat_completion(messages: list, *, model=None, temperature=0.3, max_tokens=100000,
                    timeout=60.0, retries=2, tools=None, tool_choice="auto",
                    use_proxy=False) -> dict:
    """OpenAI 兼容 chat completions,返回完整 API 响应(含 tool_calls)。

    用于 function calling 场景:LLM 可能返回 tool_calls 而非 content;
    调用方需自行处理 tool 执行循环并继续对话。
    """
    if not get_llm_api_key() or not get_llm_base_url():
        raise LLMError("LLM 未配置:请在 .env 设置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")

    url = get_llm_base_url().rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    url += "/chat/completions"
    headers = {
        "Authorization": f"Bearer {get_llm_api_key()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or get_llm_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    client_kwargs: dict = {"timeout": timeout}
    proxy = get_overseas_proxy() if use_proxy else None
    if proxy:
        client_kwargs["proxy"] = proxy

    last_exc = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(**client_kwargs) as c:
                r = c.post(url, headers=headers, json=payload)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"LLM 调用失败(重试 {retries + 1} 次):{type(last_exc).__name__}: {last_exc}")


def chat_json(system: str, user: str, **kw) -> dict:
    """调 LLM 并 json_repair 解析为 dict。供顾问 parse 用。"""
    from json_repair import repair_json

    raw = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        **kw,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(repair_json(raw))
