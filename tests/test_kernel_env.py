from __future__ import annotations

import inspect
import json
import shlex
from pathlib import Path
from typing import Any

import httpx
import pytest

import bb_harbor.remote_cdp_env as kernel_module
from bb_harbor import TRAJECTORIES_ROOT
from bb_harbor.kernel_env import KernelCredentialError, KernelEnvironment
from harbor.environments.base import ExecResult
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths


INSTRUCTION = "stagehand-task-id: agent/columbia_tuition"
SUCCESSFUL_EVAL_PREVIEW = "  Target: agent/columbia_tuition  →  core (1 task)\n"


class RecordingKernelEnvironment(KernelEnvironment):
    """Kernel environment double that keeps the real session scope and merge."""

    def __init__(self, *args, script=None, **kwargs) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script = script
        super().__init__(*args, **kwargs)

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        merged_env = self._merge_env(env) or {}
        self.calls.append({"command": command, "env": merged_env})
        if self.script is None:
            return ExecResult(return_code=0)
        result = self.script(command, merged_env)
        if inspect.isawaitable(result):
            result = await result
        return result


class _KernelApi:
    """httpx MockTransport handler recording Kernel API calls."""

    def __init__(self, create_status: int = 200, delete_status: int = 204) -> None:
        self.create_status = create_status
        self.delete_status = delete_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/browsers":
            if self.create_status >= 400:
                return httpx.Response(self.create_status, json={"message": "nope"})
            return httpx.Response(
                200,
                json={
                    "session_id": "kernel-session",
                    "cdp_ws_url": "wss://cdp.example/kernel-session?jwt=secret",
                },
            )
        if request.method == "DELETE":
            return httpx.Response(self.delete_status)
        return httpx.Response(200, json=[])


@pytest.fixture
def kernel_env_factory(tmp_path, monkeypatch):
    async def noop_start(self, force_build: bool) -> None:
        del self, force_build

    stops: list[bool] = []

    async def record_stop(self, delete: bool) -> None:
        del self
        stops.append(delete)

    monkeypatch.setattr(DockerEnvironment, "start", noop_start)
    monkeypatch.setattr(DockerEnvironment, "stop", record_stop)
    monkeypatch.setenv("KERNEL_API_KEY", "kernel-key")

    def factory(
        api: _KernelApi | None = None, *, script=None, **kwargs
    ) -> RecordingKernelEnvironment:
        environment_dir = tmp_path / "environment"
        environment_dir.mkdir(exist_ok=True)
        (environment_dir / "Dockerfile").write_text("FROM scratch\n")
        environment = RecordingKernelEnvironment(
            environment_dir=environment_dir,
            environment_name="env-kernel",
            session_id="trial__env",
            trial_paths=TrialPaths(tmp_path / "trial"),
            task_env_config=EnvironmentConfig(),
            script=script,
            **kwargs,
        )
        if api is not None:
            environment._http_client = httpx.AsyncClient(
                base_url="https://api.onkernel.com",
                transport=httpx.MockTransport(api),
            )
        environment.docker_stops = stops
        return environment

    return factory


def test_kernel_environment_is_concrete_and_not_browserbase():
    assert KernelEnvironment.__abstractmethods__ == frozenset()
    assert KernelEnvironment.uses_browserbase is False
    assert KernelEnvironment.uses_remote_cdp is True
    assert KernelEnvironment.type() == "kernel-docker"


@pytest.mark.asyncio
async def test_start_creates_browser_and_stop_deletes_it(kernel_env_factory):
    api = _KernelApi()
    environment = kernel_env_factory(api, timeout_seconds=120, stealth=True)

    await environment.start(force_build=False)

    create = api.requests[0]
    assert create.method == "POST"
    body = json.loads(create.content)
    assert body["timeout_seconds"] == 120
    assert body["stealth"] is True
    assert body["name"] == "harbor-trial__env"
    assert environment.remote_session_id == "kernel-session"
    assert environment.session_env() == {
        "STAGEHAND_CDP_URL": "wss://cdp.example/kernel-session?jwt=secret"
    }

    await environment.stop(delete=True)

    assert api.requests[-1].method == "DELETE"
    assert api.requests[-1].url.path == "/browsers/kernel-session"
    assert environment.remote_session_id is None
    assert environment.remote_cdp_url is None
    assert environment.docker_stops == [True]


@pytest.mark.asyncio
async def test_start_failure_tears_down_docker(kernel_env_factory):
    environment = kernel_env_factory(_KernelApi(create_status=402))

    with pytest.raises(RuntimeError, match="HTTP 402"):
        await environment.start(force_build=False)

    assert environment.remote_session_id is None
    assert environment.docker_stops == [True]


@pytest.mark.asyncio
async def test_agent_attaches_eval_to_kernel_cdp_url(kernel_env_factory, agent_factory):
    def script(command, merged_env):
        if command.startswith("evals run") and "--preview" in shlex.split(command):
            return ExecResult(stdout=SUCCESSFUL_EVAL_PREVIEW, return_code=0)
        if command.startswith("find "):
            path = (
                f"{TRAJECTORIES_ROOT}/scope/actual/agent/columbia_tuition/"
                "run/trajectory.json"
            )
            return ExecResult(stdout=f"1\t10\t{path}\n", return_code=0)
        return ExecResult(return_code=0)

    environment = kernel_env_factory(_KernelApi(), script=script)
    await environment.start(force_build=False)
    agent = agent_factory(extra_env={"OPENROUTER_API_KEY": "or-key"})
    context = AgentContext()

    await agent.run(INSTRUCTION, environment, context)

    eval_call = next(
        call
        for call in environment.calls
        if call["command"].startswith("evals run")
        and "--preview" not in shlex.split(call["command"])
    )
    argv = shlex.split(eval_call["command"])
    assert "--env" not in argv
    assert eval_call["env"]["STAGEHAND_CDP_URL"].startswith("wss://cdp.example/")
    assert not any(
        call["command"].startswith("script ") for call in environment.calls
    )
    assert context.metadata["stagehand"]["kernel_session_id"] == "kernel-session"
    artifact = json.loads(
        (Path(agent.logs_dir) / "kernel_session.json").read_text(encoding="utf-8")
    )
    assert artifact == {
        "provider": "kernel",
        "session_id": "kernel-session",
        "task_id": "agent/columbia_tuition",
        "mode": "dom",
    }
    assert "secret" not in json.dumps(artifact)


def _prepare_preflight(monkeypatch, status_code: int) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return httpx.Response(status_code, request=httpx.Request("GET", url))

    def docker_preflight(cls) -> None:
        del cls

    monkeypatch.setattr(DockerEnvironment, "preflight", classmethod(docker_preflight))
    monkeypatch.setattr(kernel_module.httpx, "get", fake_get)
    monkeypatch.setenv("KERNEL_API_KEY", "kernel-key")
    monkeypatch.delenv("KERNEL_SKIP_PREFLIGHT_API_CHECK", raising=False)
    monkeypatch.delenv("KERNEL_BASE_URL", raising=False)
    return calls


def test_preflight_requires_api_key(monkeypatch):
    _prepare_preflight(monkeypatch, 200)
    monkeypatch.delenv("KERNEL_API_KEY")

    with pytest.raises(RuntimeError, match="KERNEL_API_KEY"):
        KernelEnvironment.preflight()


@pytest.mark.parametrize("status_code", [401, 403])
def test_preflight_classifies_rejected_credentials(monkeypatch, status_code):
    _prepare_preflight(monkeypatch, status_code)

    with pytest.raises(KernelCredentialError):
        KernelEnvironment.preflight()


def test_preflight_lists_browsers_with_bearer_key(monkeypatch):
    calls = _prepare_preflight(monkeypatch, 200)

    KernelEnvironment.preflight()

    assert calls[0]["url"] == "https://api.onkernel.com/browsers"
    assert calls[0]["headers"] == {"Authorization": "Bearer kernel-key"}
