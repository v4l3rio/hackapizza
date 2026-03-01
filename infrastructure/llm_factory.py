"""
Singleton factory for the shared LLM client.
Uses the OpenAI-compatible Regolo API via datapizza-ai's OpenAILikeClient.
"""
from __future__ import annotations

from datapizza.clients.openai_like import OpenAILikeClient

import config

_client: OpenAILikeClient | None = None


def get_llm_client() -> OpenAILikeClient:
    """Return (and lazily create) the shared LLM client."""
    global _client
    if _client is None:
        _client = OpenAILikeClient(
            api_key=config.REGOLO_API_KEY,
            model=config.REGOLO_MODEL,
            base_url=config.REGOLO_BASE_URL,
        )
    return _client

def get_llm_client_small() -> OpenAILikeClient:
    """Return a separate LLM client for small models."""
    return OpenAILikeClient(
        api_key=config.REGOLO_API_KEY,
        model=config.REGOLO_SMALL_MODEL,
        base_url=config.REGOLO_BASE_URL,
    )