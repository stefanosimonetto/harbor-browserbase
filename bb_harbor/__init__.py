"""Browserbase-backed Harbor integrations."""

from typing import TYPE_CHECKING

TRAJECTORIES_ROOT = "/logs/agent/trajectories"
TRAJECTORY_POINTER_PATH = "/logs/agent/stagehand-trajectory-dir"

__all__ = [
    "AnchorEnvironment",
    "BrowserbaseDockerEnvironment",
    "BrowserbaseEnvironment",
    "KernelEnvironment",
    "StagehandAgent",
    "StagehandVerifier",
    "TRAJECTORIES_ROOT",
    "TRAJECTORY_POINTER_PATH",
]

if TYPE_CHECKING:
    from .agent import StagehandAgent
    from .anchor_env import AnchorEnvironment
    from .env import BrowserbaseDockerEnvironment, BrowserbaseEnvironment
    from .kernel_env import KernelEnvironment
    from .verifier import StagehandVerifier


def __getattr__(name: str):
    if name in {"BrowserbaseDockerEnvironment", "BrowserbaseEnvironment"}:
        from .env import BrowserbaseDockerEnvironment, BrowserbaseEnvironment

        value = (
            BrowserbaseDockerEnvironment
            if name == "BrowserbaseDockerEnvironment"
            else BrowserbaseEnvironment
        )
    elif name == "AnchorEnvironment":
        from .anchor_env import AnchorEnvironment

        value = AnchorEnvironment
    elif name == "KernelEnvironment":
        from .kernel_env import KernelEnvironment

        value = KernelEnvironment
    elif name == "StagehandAgent":
        from .agent import StagehandAgent

        value = StagehandAgent
    elif name == "StagehandVerifier":
        from .verifier import StagehandVerifier

        value = StagehandVerifier
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value
