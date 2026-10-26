from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import httpx
import pytest

import bb_harbor.remote_cdp_env as remote_module
from bb_harbor import TRAJECTORIES_ROOT
from bb_harbor.anchor_env import AnchorEnvironment
from bb_harbor.remote_cdp_env import RemoteBrowserCredentialError
from harbor.environments.base import ExecResult
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from test_kernel_env import INSTRUCTION, SUCCESSFUL_EVAL_PREVIEW


class RecordingAnchorEnvironment(AnchorEnvironment):
    def __init__(self, *args, script=None, **kwargs) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script = script
        super().__init__(*args, **kwargs)

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        merged_env = self._merge_env(env) or {}
        self.calls.append({"command": command, "env": merged_env})
        if self.script is None:
            return ExecResult(return_code=0)
        return self.script(command, merged_env)


class _AnchorApi:
    def __init__(self, create_status: int = 200) -> None:
        self.create_status = create_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/sessions":
            if self.create_status >= 400:
                return httpx.Response(self.create_status, json={"error": {}})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "anchor-session",
                        "cdp_url": "wss://connect.anchorbrowser.io?sessionId=x&apiKey=secret",
                        "live_view_url": "https://live.anchorbrowser.io/x",
                    }
                },
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={"data": {"status": "deleted"}})
        return httpx.Response(200, json={"data": {}})


@pytest.fixture
def anchor_env_factory(tmp_path, monkeypatch):
    async def noop_start(self, force_build: bool) -> None:
        del self, force_build

    stops: list[bool] = []

    async def record_stop(self, delete: bool) -> None:
        del self
        stops.append(delete)

    monkeypatch.setattr(DockerEnvironment, "start", noop_start)
    monkeypatch.setattr(DockerEnvironment, "stop", record_stop)
    monkeypatch.setenv("ANCHOR_API_KEY", "anchor-key")

    def factory(api: _AnchorApi, *, script=None, **kwargs) -> RecordingAnchorEnvironment:
        environment_dir = tmp_path / "environment"
        environment_dir.mkdir(exist_ok=True)
        (environment_dir / "Dockerfile").write_text("FROM scratch\n")
        environment = RecordingAnchorEnvironment(
            environment_dir=environment_dir,
            environment_name="env-anchor",
            session_id="trial__env",
            trial_paths=TrialPaths(tmp_path / "trial"),
            task_env_config=EnvironmentConfig(),
            script=script,
            **kwargs,
        )
        environment._http_client = httpx.AsyncClient(
            base_url="https://api.anchorbrowser.io",
            headers=AnchorEnvironment._auth_headers(),
            transport=httpx.MockTransport(api),
        )
        environment.docker_stops = stops
        return environment

    return factory


def test_anchor_environment_is_concrete_and_not_browserbase():
    assert AnchorEnvironment.__abstractmethods__ == frozenset()
    assert AnchorEnvironment.uses_browserbase is False
    assert AnchorEnvironment.uses_remote_cdp is True
    assert AnchorEnvironment.type() == "anchor-docker"


@pytest.mark.asyncio
async def test_start_creates_session_with_blockers_off_and_stop_ends_it(
    anchor_env_factory,
):
    api = _AnchorApi()
    environment = anchor_env_factory(api)

    await environment.start(force_build=False)

    create = api.requests[0]
    assert create.headers["anchor-api-key"] == "anchor-key"
    body = json.loads(create.content)
    assert body["browser"]["adblock"] == {"active": False}
    assert body["browser"]["popup_blocker"] == {"active": False}
    assert body["session"]["timeout"] == {"max_duration": 25, "idle_timeout": 5}
    assert body["session"]["tags"] == ["harbor-trial__env"]
    assert environment.remote_session_id == "anchor-session"
    assert environment.session_env()["STAGEHAND_CDP_URL"].startswith("wss://connect.")

    await environment.stop(delete=True)

    assert api.requests[-1].method == "DELETE"
    assert api.requests[-1].url.path == "/v1/sessions/anchor-session"
    assert environment.remote_session_id is None
    assert environment.docker_stops == [True]


@pytest.mark.asyncio
async def test_start_failure_tears_down_docker(anchor_env_factory):
    environment = anchor_env_factory(_AnchorApi(create_status=402))

    with pytest.raises(RuntimeError, match="HTTP 402"):
        await environment.start(force_build=False)

    assert environment.docker_stops == [True]


@pytest.mark.asyncio
async def test_agent_attaches_eval_to_anchor_cdp_url(anchor_env_factory, agent_factory):
    def script(command, merged_env):
        if command.startswith("evals run") and "--preview" in shlex.split(command):
            return ExecResult(stdout=SUCCESSFUL_EVAL_PREVIEW, return_code=0)
        if command.startswith("find "):
            path = f"{TRAJECTORIES_ROOT}/scope/a/agent/columbia_tuition/r/trajectory.json"
            return ExecResult(stdout=f"1\t10\t{path}\n", return_code=0)
        return ExecResult(return_code=0)

    environment = anchor_env_factory(_AnchorApi(), script=script)
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
    assert "--env" not in shlex.split(eval_call["command"])
    assert "apiKey=secret" in eval_call["env"]["STAGEHAND_CDP_URL"]
    assert context.metadata["stagehand"]["anchor_session_id"] == "anchor-session"
    artifact_text = (Path(agent.logs_dir) / "anchor_session.json").read_text(
        encoding="utf-8"
    )
    assert json.loads(artifact_text)["provider"] == "anchor"
    assert "secret" not in artifact_text


def test_preflight_uses_anchor_header(monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return httpx.Response(401, request=httpx.Request("GET", url))

    monkeypatch.setattr(
        DockerEnvironment, "preflight", classmethod(lambda cls: None)
    )
    monkeypatch.setattr(remote_module.httpx, "get", fake_get)
    monkeypatch.setenv("ANCHOR_API_KEY", "anchor-key")
    monkeypatch.delenv("ANCHOR_SKIP_PREFLIGHT_API_CHECK", raising=False)

    with pytest.raises(RemoteBrowserCredentialError):
        AnchorEnvironment.preflight()

    assert calls[0]["url"] == "https://api.anchorbrowser.io/v1/sessions/all/status"
    assert calls[0]["headers"] == {"anchor-api-key": "anchor-key"}
