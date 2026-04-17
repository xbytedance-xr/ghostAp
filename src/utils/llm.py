from __future__ import annotations

import threading
from typing import MutableMapping

from langchain_openai import ChatOpenAI


ChatOpenAICacheKey = tuple[object, str, str, float, int]
_CACHE_LOCK = threading.Lock()


def _cache_key(settings, temperature: float, llm_cls: object) -> ChatOpenAICacheKey:
    return (
        llm_cls,
        settings.ark_base_url,
        settings.ark_model,
        float(temperature),
        hash(settings.ark_api_key or ""),
    )


def _build_chat_openai(settings, temperature: float, llm_cls: type[ChatOpenAI]) -> ChatOpenAI:
    return llm_cls(
        base_url=settings.ark_base_url,
        api_key=settings.ark_api_key,
        model=settings.ark_model,
        temperature=temperature,
    )


def get_cached_chat_openai(
    settings,
    temperature: float,
    *,
    cache: MutableMapping[ChatOpenAICacheKey, ChatOpenAI],
    llm_cls: type[ChatOpenAI] = ChatOpenAI,
    maxsize: int = 8,
) -> ChatOpenAI:
    key = _cache_key(settings, temperature, llm_cls)
    with _CACHE_LOCK:
        llm = cache.get(key)
        if llm is not None:
            cache.pop(key, None)
            cache[key] = llm
            return llm

        llm = _build_chat_openai(settings, temperature, llm_cls)
        cache[key] = llm
        while len(cache) > max(1, maxsize):
            cache.pop(next(iter(cache)))
        return llm
