"""A Harbor Docker environment that runs Stagehand evals against Anchor Browser.

Anchor Browser (https://anchorbrowser.io) hosts Chromium sessions reachable over
CDP. See ``remote_cdp_env`` for how the CDP URL reaches Stagehand.

Anchor enables ad-blocking and popup/consent-banner blocking by default, which
neither Browserbase nor Kernel do. Both default to off here so pages look the
same across backends; turn them on explicitly to measure their effect. Anchor's
own ``max_duration`` (default 20 minutes) must exceed the agent timeout (900 s).
"""

from __future__ import annotations

import os
from typing import Any, override

from bb_harbor.remote_cdp_env import RemoteCdpEnvironment


DEFAULT_ANCHOR_BASE_URL = "https://api.anchorbrowser.io"


class AnchorEnvironment(RemoteCdpEnvironment):
    """Pair a Harbor Docker container with one Anchor Browser session per trial."""

    provider = "Anchor"
    api_key_env = "ANCHOR_API_KEY"
    base_url_env = "ANCHOR_BASE_URL"
    default_base_url = DEFAULT_ANCHOR_BASE_URL
    skip_preflight_env = "ANCHOR_SKIP_PREFLIGHT_API_CHECK"
    create_path = "/v1/sessions"
    delete_path = "/v1/sessions/{session_id}"
    preflight_path = "/v1/sessions/all/status"

    def __init__(
        self,
        *args: Any,
        headless: bool = False,
        adblock: bool = False,
        popup_blocker: bool = False,
        recording: bool = False,
        max_duration_minutes: int = 25,
        idle_timeout_minutes: int = 5,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if max_duration_minutes < 1 or idle_timeout_minutes < 1:
            raise ValueError("Anchor timeouts must be at least one minute")
        self.headless: bool = headless
        self.adblock: bool = adblock
        self.popup_blocker: bool = popup_blocker
        self.recording: bool = recording
        self.max_duration_minutes: int = max_duration_minutes
        self.idle_timeout_minutes: int = idle_timeout_minutes

    @classmethod
    @override
    def _auth_headers(cls) -> dict[str, str]:
        return {"anchor-api-key": os.environ[cls.api_key_env]}

    @staticmethod
    @override
    def type() -> str:
        return "anchor-docker"

    @override
    def _create_payload(self) -> dict[str, Any]:
        return {
            "session": {
                # Tagging with Harbor's identity makes an unrecorded create findable.
                "tags": [f"harbor-{self.session_id}"[:100]],
                "recording": {"active": self.recording},
                "timeout": {
                    "max_duration": self.max_duration_minutes,
                    "idle_timeout": self.idle_timeout_minutes,
                },
            },
            "browser": {
                "headless": {"active": self.headless},
                "adblock": {"active": self.adblock},
                "popup_blocker": {"active": self.popup_blocker},
            },
        }

    @staticmethod
    @override
    def _parse_created(payload: dict[str, Any]) -> tuple[str | None, str | None]:
        data = payload.get("data") or {}
        return data.get("id"), data.get("cdp_url")


__all__ = [
    "AnchorEnvironment",
    "DEFAULT_ANCHOR_BASE_URL",
]
