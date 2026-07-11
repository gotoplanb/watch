"""
Conduct provider for the AI-drafted RCA (watch#41, ADR-034).

Drafts through the `conduct` project's local models (Ollama-backed) over a **synchronous** HTTP
call — the primary models are pinned resident in Conduct, so the job runs inline and the draft
comes back in the POST response (no async job/poll). This is the local-dev backend: no AWS, no
Bedrock model-access grant. Watch owns the instruction prompt and sends it on every call
(`system_prompt`), so Conduct never stores it and the instruction can't drift from the Bedrock path.

Contract (verified against the `watch` client):
  POST {CONDUCT_BASE_URL}/jobs   Authorization: Bearer {CONDUCT_API_KEY}
    { "task_type": "...", "system_prompt": "...", "prompt": "..." }
  -> 200 { "status": "complete", "model_used": "...", "response": "...", "job_id": "...", ... }
"""
import logging

import requests
from django.conf import settings

from .rca_ai import DraftError, DraftResult

logger = logging.getLogger(__name__)


def draft(system_prompt: str, source_markdown: str) -> DraftResult:
    """Draft via Conduct. `model` in the result is Conduct's reported `model_used` (the model that
    actually ran). Raises DraftError on any transport/HTTP/status failure."""
    if not settings.CONDUCT_API_KEY or not settings.CONDUCT_BASE_URL:
        raise DraftError("Conduct is not configured (CONDUCT_BASE_URL / CONDUCT_API_KEY)")

    url = settings.CONDUCT_BASE_URL.rstrip("/") + "/jobs"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {settings.CONDUCT_API_KEY}"},
            json={
                "task_type": settings.CONDUCT_TASK_TYPE,
                "system_prompt": system_prompt,
                "prompt": source_markdown,
            },
            timeout=settings.CONDUCT_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.exception("conduct.draft transport error url=%s", url)
        raise DraftError(f"Conduct unreachable: {exc}") from exc

    if resp.status_code >= 300:
        raise DraftError(f"Conduct HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise DraftError("Conduct returned a non-JSON body") from exc

    status = body.get("status")
    if status != "complete":
        raise DraftError(f"Conduct job not complete (status={status!r}): {body.get('error') or ''}")

    text = (body.get("response") or "").strip()
    if not text:
        raise DraftError("Conduct returned an empty draft")
    return DraftResult(text=text, provider="conduct", model=body.get("model_used") or "conduct")
