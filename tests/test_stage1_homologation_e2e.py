"""Live-only Stage 1 check that vision traffic is routed through the Toolbox.

This test must run only against the explicitly allowlisted HOMOLOG instance.
The SDK builds the gateway path; no model server URL/key is used in Core.
"""

from __future__ import annotations

import os

import httpx
import pytest

from acessilia_sdk.toolbox.endpoints import ToolboxEndpoint
from acessilia_sdk.toolbox.transport import auth_headers
from acessilia_sdk.toolbox.vlm import vlm_gateway_base_url, vlm_provider_healthy
from backend.config.settings import settings


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_gateway_health_and_chat_are_reached_only_via_homolog_toolbox():
    url = os.getenv("TOOLBOX_BASE_URL", "").strip()
    allowed_host = os.getenv("HOMOLOG_ALLOWED_HOST", "").strip()
    if not url or not allowed_host:
        pytest.fail("Explicit homologation URL and host allowlist are required")

    endpoint = ToolboxEndpoint(url)
    parsed_host = httpx.URL(endpoint.base_url).host
    assert parsed_host == allowed_host, "Toolbox address differs from homolog allowlist"

    provider = settings.toolbox_vlm_provider
    api_key = os.getenv("TOOLBOX_API_KEY")
    assert provider, "HOMOLOG VLM provider is not configured"
    assert await vlm_provider_healthy(
        endpoint, provider, api_key=api_key, timeout_seconds=15
    ), "HOMOLOG VLM gateway reports the selected provider unavailable"

    # The one small text-only request verifies the actual chat transport.
    # It enters /v1/vlm through the Toolbox; no direct llama-server fallback.
    async with httpx.AsyncClient(
        base_url=vlm_gateway_base_url(endpoint),
        timeout=httpx.Timeout(120.0),
        headers=auth_headers(api_key),
    ) as http:
        response = await http.post(
            "chat/completions",
            json={
                "model": provider,
                "messages": [{"role": "user", "content": "Responda apenas OK."}],
                "max_tokens": 20,
                "stream": False,
            },
        )
        response.raise_for_status()
        content = response.json()
    choices = content.get("choices")
    assert isinstance(choices, list) and choices
    assert str((choices[0].get("message") or {}).get("content") or "").strip()
