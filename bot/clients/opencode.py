import asyncio
import base64
import tempfile
from pathlib import Path

import httpx

from bot.utils.logger import logger
from config.settings import settings


class OpenCodeClient:
    """HTTP client for the OpenCode serve API."""

    def __init__(self):
        self.base_url = settings.opencode_url.rstrip("/")
        self.timeout = settings.request_timeout
        self._session_id: str | None = None
        self._temp_files: list[Path] = []

    async def health_check(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{self.base_url}/global/health")
            resp.raise_for_status()
            return resp.json()

    async def _create_session(self) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/session", json={})
            resp.raise_for_status()
            data = resp.json()
            session_id = data.get("id")
            if not session_id:
                raise RuntimeError(f"Failed to create session: {data}")
            logger.info("OpenCode session created: {}", session_id)
            return session_id

    async def _get_session(self) -> str:
        if self._session_id:
            return self._session_id
        self._session_id = await self._create_session()
        return self._session_id

    def _image_to_data_uri(self, img_bytes: bytes) -> str:
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"

    def _save_image_to_temp(self, img_bytes: bytes) -> Path:
        tmp = Path(tempfile.gettempdir())
        f = tmp / f"opencode_img_{len(self._temp_files):04d}.jpg"
        f.write_bytes(img_bytes)
        self._temp_files.append(f)
        return f

    def _cleanup_temp_files(self):
        for f in self._temp_files:
            if f.exists():
                f.unlink()
        self._temp_files.clear()

    async def _post_with_retry(
        self,
        url: str,
        json: dict,
        max_retries: int = 3,
        base_delay: float = 2.0,
    ) -> dict:
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=json)
                    if resp.status_code >= 400:
                        logger.error("OpenCode error response ({}): {}", resp.status_code, resp.text[:500])
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.warning("Session expired (404), recreating session and retrying...")
                    self._session_id = None
                    new_session = await self._get_session()
                    url = f"{self.base_url}/session/{new_session}/message"
                    continue
                elif e.response.status_code == 500 and attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning("OpenCode 500 error, retrying in {}s (attempt {}/{})", delay, attempt, max_retries)
                    await asyncio.sleep(delay)
                    continue
                else:
                    raise
            except (httpx.ReadError, httpx.ConnectError) as e:
                last_error = e
                if attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning("Connection error ({}), retrying in {}s (attempt {}/{})", type(e).__name__, delay, attempt, max_retries)
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error("Connection failed after {} attempts: {}", max_retries, last_error)
                    raise
        raise last_error

    async def send_message(
        self,
        text: str,
        images: list[bytes] | None = None,
        max_empty_retries: int = 2,
    ) -> str:
        parts: list[dict] = []

        if images:
            for img_bytes in images:
                data_uri = self._image_to_data_uri(img_bytes)
                parts.append({
                    "type": "file",
                    "url": data_uri,
                    "mime": "image/jpeg",
                })
                logger.debug("Image encoded as data URI ({} bytes -> {} chars)", len(img_bytes), len(data_uri))

        parts.append({"type": "text", "text": text})

        session_id = await self._get_session()

        payload = {
            "parts": parts,
            "model": {"modelID": settings.opencode_model, "providerID": "opencode"},
        }

        logger.debug("OpenCode payload: parts={}, model={}, image_count={}", len(parts), settings.opencode_model, len(images or []))

        url = f"{self.base_url}/session/{session_id}/message"
        data = await self._post_with_retry(url, payload)

        result = self._extract_text(data)

        if not result and max_empty_retries > 0:
            logger.warning("OpenCode returned empty response (session {}), resetting session and retrying...", session_id)
            self._session_id = None
            return await self.send_message(text, images, max_empty_retries=max_empty_retries - 1)

        return result

    @staticmethod
    def _extract_text(data: dict) -> str:
        parts = data.get("parts", [])
        texts = []
        for part in parts:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
        result = "\n".join(texts).strip()
        if result:
            logger.info("OpenCode response: {} chars", len(result))
        return result

    def reset_session(self):
        self._session_id = None
        self._cleanup_temp_files()


client = OpenCodeClient()
