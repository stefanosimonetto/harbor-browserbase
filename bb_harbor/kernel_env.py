"""A Harbor Docker environment that runs Stagehand evals against a Kernel browser.

Kernel (https://kernel.sh) hosts Chromium browsers reachable over CDP. See
``remote_cdp_env`` for how the CDP URL reaches Stagehand. Kernel deletes an idle
browser after ``timeout_seconds`` without a CDP connection (default 60 on
Kernel's side, 300 here), which also covers the gap before Stagehand attaches.
"""

from __future__ import annotations

from typing import Any, override

from bb_harbor.remote_cdp_env import RemoteBrowserCredentialError, RemoteCdpEnvironment


DEFAULT_KERNEL_BASE_URL = "https://api.onkernel.com"

# Kept for callers written against the Kernel-only module.
KernelCredentialError = RemoteBrowserCredentialError


class KernelEnvironment(RemoteCdpEnvironment):
    """Pair a Harbor Docker container with one Kernel cloud browser per trial."""

    provider = "Kernel"
    api_key_env = "KERNEL_API_KEY"
    base_url_env = "KERNEL_BASE_URL"
    default_base_url = DEFAULT_KERNEL_BASE_URL
    skip_preflight_env = "KERNEL_SKIP_PREFLIGHT_API_CHECK"
    create_path = "/browsers"
    delete_path = "/browsers/{session_id}"
    preflight_path = "/browsers"
    preflight_params = {"limit": 1}

    def __init__(
        self,
        *args: Any,
        headless: bool = False,
        stealth: bool = False,
        timeout_seconds: int = 300,
        region: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if not 10 <= timeout_seconds <= 259_200:
            raise ValueError("timeout_seconds must be between 10 and 259200")
        self.headless: bool = headless
        self.stealth: bool = stealth
        self.timeout_seconds: int = timeout_seconds
        self.region: str | None = region

    @staticmethod
    @override
    def type() -> str:
        return "kernel-docker"

    @override
    def _create_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "headless": self.headless,
            "stealth": self.stealth,
            "timeout_seconds": self.timeout_seconds,
            # Tagging with Harbor's identity makes an unrecorded create findable.
            "name": f"harbor-{self.session_id}"[:255],
        }
        if self.region:
            payload["region"] = self.region
        return payload

    @staticmethod
    @override
    def _parse_created(payload: dict[str, Any]) -> tuple[str | None, str | None]:
        return payload.get("session_id"), payload.get("cdp_ws_url")


__all__ = [
    "DEFAULT_KERNEL_BASE_URL",
    "KernelCredentialError",
    "KernelEnvironment",
]
