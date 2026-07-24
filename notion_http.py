"""Shared HTTP retry/backoff policy for every Notion API call in this project.

Used by notion.py, notionconfig.py, notionaccounts.py, and missing.py — each
targets a different Notion API version (the new data-source API for the Capture
Log, the legacy API for the other databases), but the retry policy is identical:
honor 429 `Retry-After`, retry 5xx, and retry network-level errors (timeouts,
connection resets) that would otherwise bypass retry entirely and crash the call.
"""
from __future__ import annotations

import random
import time

import requests


def send(method: str, url: str, headers_fn, *, attempts: int = 8, timeout: int = 30,
        **kw) -> requests.Response:
    """One Notion request with backoff. `headers_fn` is called fresh on every
    attempt (so a rotated token takes effect without a restart). Re-raises the
    last network exception only if every attempt fails; otherwise returns the
    final HTTP response (which may still be a non-2xx the caller must check)."""
    r = None
    last_exc = None
    for i in range(attempts):
        try:
            r = requests.request(method, url, headers=headers_fn(), timeout=timeout, **kw)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(min(30.0, 1.0 * (2 ** i)) + random.uniform(0, 0.5))
            continue
        last_exc = None
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", 0) or 0) or (1.0 * (2 ** i))
            time.sleep(min(wait, 240.0) + random.uniform(0, 0.5))
            continue
        if r.status_code >= 500:
            time.sleep(min(30.0, 1.0 * (2 ** i)) + random.uniform(0, 0.5))
            continue
        return r
    if last_exc is not None:
        raise last_exc
    return r
