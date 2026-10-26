"""Shared Harbor Docker environment for hosted browsers reached over CDP.

Providers such as Kernel and Anchor Browser host Chromium and hand back a CDP
WebSocket URL. Stagehand does not create these browsers itself: a subclass
creates one browser per trial in ``start()`` and this base exposes its CDP URL
as ``STAGEHAND_CDP_URL`` through ``session_scope()``. The task image's
``remote-cdp.patch`` makes ``benchHarness`` attach to that URL in LOCAL mode
instead of launching Chrome or creating a Browserbase session.

Stagehand's LOCAL attach path installs a no-op ``kill`` for external browsers
(``packages/core/lib/v3/v3.ts`` ~985), so closing V3 only disconnects. The
browser is released in ``stop()``; each provider's idle timeout is the backstop
if Harbor dies before that.

The CDP URL is injected per exec with ``scoped_exec_env`` and never persisted by
this module. It is a live credential for the session until the browser is
released.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Generator
from typing import Any, ClassVar, override

import httpx
from harbor.environments.docker.docker import DockerEnvironment


HTTP_TIMEOUT_SEC = 60.0


class RemoteBrowserCredentialError(RuntimeError):
    """A rejected or missing hosted-browser API key."""


class RemoteCdpEnvironment(DockerEnvironment):
    """Pair a Harbor Docker container with one hosted CDP browser per trial.

    Subclasses set the class attributes and implement ``_create_payload`` and
    ``_parse_created``; the HTTP, cancellation, and teardown handling is shared.
    """

    # Read by StagehandAgent: never pass ``--env browserbase``; enter
    # ``session_scope()`` so the eval attaches to the hosted browser.
    uses_browserbase: bool = False
    uses_remote_cdp: bool = True

    provider: ClassVar[str]
    api_key_env: ClassVar[str]
    base_url_env: ClassVar[str]
    default_base_url: ClassVar[str]
    skip_preflight_env: ClassVar[str]
    create_path: ClassVar[str]
    delete_path: ClassVar[str]  # formatted with ``session_id``
    preflight_path: ClassVar[str]
    preflight_params: ClassVar[dict[str, Any]] = {}

    def __init__(
        self,
        *args: Any,
        delete_on_start_failure: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.delete_on_start_failure: bool = delete_on_start_failure
        self.remote_session_id: str | None = None
        self.remote_cdp_url: str | None = None
        self._http_client: httpx.AsyncClient | None = None

    # Provider hooks -------------------------------------------------------

    @classmethod
    def _auth_headers(cls) -> dict[str, str]:
        return {"Authorization": f"Bearer {os.environ[cls.api_key_env]}"}

    def _create_payload(self) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _parse_created(payload: dict[str, Any]) -> tuple[str | None, str | None]:
        """Return ``(session_id, cdp_url)`` from a create response body."""

        raise NotImplementedError

    # Shared plumbing ------------------------------------------------------

    @classmethod
    def _base_url(cls) -> str:
        return (
            os.environ.get(cls.base_url_env, "").strip() or cls.default_base_url
        ).rstrip("/")

    @classmethod
    @override
    def preflight(cls) -> None:
        """Validate the API key with one cheap authenticated GET."""

        super().preflight()
        if not os.environ.get(cls.api_key_env, "").strip():
            raise RuntimeError(
                f"Missing required {cls.provider} environment variable: "
                f"{cls.api_key_env}"
            )

        if os.environ.get(cls.skip_preflight_env, "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            return

        try:
            response = httpx.get(
                f"{cls._base_url()}{cls.preflight_path}",
                params=cls.preflight_params,
                headers=cls._auth_headers(),
                timeout=HTTP_TIMEOUT_SEC,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"{cls.provider} credentials could not be checked because the API "
                "could not be reached."
            ) from exc
        if response.status_code in (401, 403):
            raise RemoteBrowserCredentialError(
                f"{cls.provider} rejected the credential configured by "
                f"{cls.api_key_env} with HTTP {response.status_code}; the "
                "credential must be replaced."
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{cls.provider} credentials could not be checked because the API "
                f"returned HTTP {response.status_code}."
            )

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self._base_url(),
                headers=self._auth_headers(),
                timeout=HTTP_TIMEOUT_SEC,
            )
        return self._http_client

    async def _close_client(self) -> None:
        client = self._http_client
        self._http_client = None
        if client is None:
            return
        try:
            await client.aclose()
        except BaseException:
            self.logger.exception("Failed to close the %s client", self.provider)

    async def _create_browser(self) -> dict[str, Any]:
        response = await self._client().post(
            self.create_path, json=self._create_payload()
        )
        if response.status_code in (401, 403):
            raise RemoteBrowserCredentialError(
                f"{self.provider} rejected browser creation with HTTP "
                f"{response.status_code}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{self.provider} browser creation failed with HTTP "
                f"{response.status_code}: {response.text[:500]}"
            )
        return response.json()

    def _record_created_browser(self, payload: dict[str, Any]) -> None:
        session_id, cdp_url = self._parse_created(payload)
        if session_id:
            self.remote_session_id = str(session_id)
        if not session_id or not cdp_url:
            raise RuntimeError(
                f"{self.provider} browser creation returned no session id or CDP URL"
            )
        self.remote_cdp_url = str(cdp_url)
        self.logger.info(
            "Created %s browser session %s", self.provider, self.remote_session_id
        )

    @override
    async def start(self, force_build: bool) -> None:
        await super().start(force_build)
        try:
            create_task = asyncio.create_task(self._create_browser())
            try:
                payload = await asyncio.shield(create_task)
            except asyncio.CancelledError:
                # Let an in-flight response land so its id can be released later.
                try:
                    payload = await asyncio.shield(create_task)
                except BaseException:
                    pass
                else:
                    self._record_created_browser(payload)
                raise
            self._record_created_browser(payload)
        except BaseException:
            if not self.remote_session_id:
                self.logger.error(
                    "Harbor session %s could not record a %s session id; check for "
                    "a leaked browser tagged harbor-%s",
                    self.session_id,
                    self.provider,
                    self.session_id,
                )
            try:
                await self._delete_browser()
            finally:
                try:
                    await super().stop(delete=self.delete_on_start_failure)
                except BaseException:
                    self.logger.exception(
                        "Docker teardown also failed after %s startup failure",
                        self.provider,
                    )
                finally:
                    await self._close_client()
            raise

    def session_env(self) -> dict[str, str]:
        return {"STAGEHAND_CDP_URL": self.remote_cdp_url} if self.remote_cdp_url else {}

    @contextlib.contextmanager
    def session_scope(self) -> Generator[None, None, None]:
        """Inject the hosted browser's CDP URL into this asyncio task's execs."""

        if not self.remote_cdp_url:
            raise RuntimeError(f"No {self.provider} browser session is available")
        with self.scoped_exec_env(self.session_env()):
            yield

    async def _delete_browser(self) -> None:
        session_id = self.remote_session_id
        if not session_id:
            return
        delete_task = asyncio.create_task(
            self._client().delete(self.delete_path.format(session_id=session_id))
        )
        try:
            response = await asyncio.shield(delete_task)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await asyncio.shield(delete_task)
            raise
        except Exception:
            self.logger.exception(
                "%s browser release failed for session %s", self.provider, session_id
            )
            return
        if response.status_code in (200, 204, 404):
            self.remote_session_id = None
            self.remote_cdp_url = None
            self.logger.info(
                "Released %s browser session %s", self.provider, session_id
            )
        else:
            self.logger.error(
                "%s browser release for session %s returned HTTP %s",
                self.provider,
                session_id,
                response.status_code,
            )

    @override
    async def stop(self, delete: bool) -> None:
        try:
            await self._delete_browser()
        finally:
            try:
                await self._close_client()
            finally:
                await super().stop(delete)


__all__ = [
    "HTTP_TIMEOUT_SEC",
    "RemoteBrowserCredentialError",
    "RemoteCdpEnvironment",
]
