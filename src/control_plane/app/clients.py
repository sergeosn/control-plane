"""Model clients for a run's roles (docs/SPEC.md sections 14 and 16).

A run asks a `ClientFactory` for one client per role, built from the role's model profile in
`plane.yaml`, before it writes anything. `environment_clients` builds the real clients with
credentials passed explicitly from the environment; `ScriptedClients` hands out one
`ScriptedClient` per role, for the tests and `cplane selfcheck`.
"""

from collections.abc import Callable, Mapping, Sequence

from control_plane.domain.plane import ModelProfile
from control_plane.llm import LLMClient, OpenAICompatibleClient, Response, ScriptedClient

ClientFactory = Callable[[str, ModelProfile], LLMClient]


class RunError(Exception):
    """A run cannot be started or resumed. The message says why, in one line."""


def environment_clients(environ: Mapping[str, str]) -> ClientFactory:
    """Real clients, with the gateway and token from `environ` (`CPLANE_LLM_*`)."""

    def build(role: str, profile: ModelProfile) -> LLMClient:
        if profile.wire_format != "openai-compatible":
            raise RunError(
                f"the {role}'s profile uses wire format {profile.wire_format}; its client is "
                "not written yet, use an openai-compatible profile"
            )
        return OpenAICompatibleClient.from_environment(
            environ,
            model=profile.model,
            max_tokens=profile.max_tokens,
            temperature=profile.temperature,
            strict=profile.strict,
        )

    return build


class ScriptedClients:
    """One scripted client per role. The same client serves every step of its role."""

    def __init__(self, scripts: Mapping[str, ScriptedClient | Sequence[Response]]) -> None:
        self.clients: dict[str, ScriptedClient] = {
            role: script if isinstance(script, ScriptedClient) else ScriptedClient(script)
            for role, script in scripts.items()
        }

    def __call__(self, role: str, profile: ModelProfile) -> LLMClient:
        try:
            return self.clients[role]
        except KeyError:
            raise RunError(f"no script for role {role}") from None
