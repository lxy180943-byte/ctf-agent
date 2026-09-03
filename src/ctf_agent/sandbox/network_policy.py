from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ctf_agent.core.config import get_nested
from ctf_agent.core.models import Challenge


@dataclass(frozen=True)
class NetworkPolicy:
    requested_network: str
    effective_network: str
    allowed: bool
    reason: str
    authorization_source: str
    connection: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_network": self.requested_network,
            "effective_network": self.effective_network,
            "allowed": self.allowed,
            "reason": self.reason,
            "authorization_source": self.authorization_source,
            "connection": self.connection,
        }


def docker_network_policy(config: dict[str, Any], challenge: Challenge) -> NetworkPolicy:
    requested = str(get_nested(config, ("sandbox", "network")) or "none")
    requested = requested.strip() or "none"
    if requested == "none":
        return NetworkPolicy(
            requested_network=requested,
            effective_network="none",
            allowed=False,
            reason="Docker network is disabled by default.",
            authorization_source="sandbox.network=none",
            connection=challenge.connection,
        )

    explicit_allow = _as_bool(get_nested(config, ("sandbox", "allow_network"))) or _as_bool(
        get_nested(config, ("sandbox", "allow_challenge_network"))
    )
    category = (challenge.category or "").lower()
    has_connection = bool((challenge.connection or "").strip())
    if not explicit_allow:
        return NetworkPolicy(
            requested_network=requested,
            effective_network="none",
            allowed=False,
            reason="Requested Docker network was denied because sandbox.allow_network is not true.",
            authorization_source="config-gate-disabled",
            connection=challenge.connection,
        )
    if not has_connection and category != "web":
        return NetworkPolicy(
            requested_network=requested,
            effective_network="none",
            allowed=False,
            reason="Requested Docker network was denied because the challenge has no connection and is not category=web.",
            authorization_source="missing-challenge-authorization",
            connection=challenge.connection,
        )
    source = "challenge.connection" if has_connection else "challenge.category=web"
    return NetworkPolicy(
        requested_network=requested,
        effective_network=requested,
        allowed=True,
        reason=f"Docker network explicitly allowed for authorized {source}.",
        authorization_source=source,
        connection=challenge.connection,
    )


def local_executor_network_note(config: dict[str, Any], challenge: Challenge) -> NetworkPolicy:
    category = (challenge.category or "").lower()
    has_connection = bool((challenge.connection or "").strip())
    explicit_allow = _as_bool(get_nested(config, ("sandbox", "allow_network"))) or _as_bool(
        get_nested(config, ("sandbox", "allow_challenge_network"))
    )
    allowed = explicit_allow and (has_connection or category == "web")
    source = "challenge.connection" if has_connection else ("challenge.category=web" if category == "web" else "none")
    reason = (
        "Local executor has no network namespace; LLM/tool risk guards must keep network commands scoped."
        if allowed
        else "Local executor has no network namespace and no explicit challenge network authorization is configured."
    )
    return NetworkPolicy(
        requested_network="local-host",
        effective_network="local-host",
        allowed=allowed,
        reason=reason,
        authorization_source=source,
        connection=challenge.connection,
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
