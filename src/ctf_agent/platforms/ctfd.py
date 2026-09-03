from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ctf_agent.core.config import get_nested
from ctf_agent.core.models import Artifact, Challenge
from ctf_agent.platforms.base import PlatformAdapter, SubmissionResult


class CTFdError(RuntimeError):
    """Base CTFd adapter error."""


class CTFdConfigError(CTFdError):
    """Raised when a CTFd profile is missing required configuration."""


class CTFdNetworkError(CTFdError):
    """Raised after retryable network failures are exhausted."""


class CTFdRateLimitError(CTFdError):
    """Raised when the platform keeps returning rate-limit responses."""


class CTFdAPIError(CTFdError):
    """Raised when CTFd returns a malformed or unsuccessful API response."""


@dataclass(frozen=True)
class CTFdProfile:
    name: str
    url: str
    token: str = ""
    team: str = ""
    flag_format: str = ""
    submit_enabled: bool = False
    retries: int = 3
    timeout: int = 30

    def redacted(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "token": "<set>" if self.token else "<missing>",
            "team": self.team,
            "flag_format": self.flag_format,
            "submit_enabled": self.submit_enabled,
            "retries": self.retries,
            "timeout": self.timeout,
        }


@dataclass(frozen=True)
class DownloadInfo:
    url: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "size": self.size, "sha256": self.sha256}


class CTFdTransport(Protocol):
    def get_json(self, path: str) -> dict[str, Any]:
        ...

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    def download(self, url: str, destination: Path) -> DownloadInfo | None:
        ...


@dataclass
class UrllibCTFdTransport:
    base_url: str
    token: str
    timeout: int = 30
    retries: int = 3
    retry_sleep: float = 0.5

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(url, data=data, method=method)
            self._add_headers(request)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - authorized CTF platform only.
                    return _decode_json(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    last_error = exc
                    self._sleep_for_retry(exc, attempt)
                    continue
                body = exc.read().decode("utf-8", errors="replace")
                raise CTFdAPIError(f"CTFd API HTTP {exc.code} for {path}: {body[:500]}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                self._sleep_for_retry(None, attempt)
        if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429:
            raise CTFdRateLimitError(f"CTFd API rate limited after {self.retries} attempt(s): {path}") from last_error
        raise CTFdNetworkError(f"CTFd API request failed after {self.retries} attempt(s): {path}") from last_error

    def get_json(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, payload)

    def download(self, url: str, destination: Path) -> DownloadInfo:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(url)
            self._add_headers(request)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - authorized CTF platform only.
                    data = response.read()
                destination.write_bytes(data)
                return _download_info(url, destination)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    last_error = exc
                    self._sleep_for_retry(exc, attempt)
                    continue
                body = exc.read().decode("utf-8", errors="replace")
                raise CTFdAPIError(f"CTFd download HTTP {exc.code} for {url}: {body[:500]}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                self._sleep_for_retry(None, attempt)
        if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429:
            raise CTFdRateLimitError(f"CTFd download rate limited after {self.retries} attempt(s): {url}") from last_error
        raise CTFdNetworkError(f"CTFd download failed after {self.retries} attempt(s): {url}") from last_error

    def _add_headers(self, request: urllib.request.Request) -> None:
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", "ctf-agent/0.1 local-authorized-client")
        if self.token:
            request.add_header("Authorization", f"Token {self.token}")

    def _sleep_for_retry(self, error: urllib.error.HTTPError | None, attempt: int) -> None:
        if attempt >= self.retries:
            return
        delay = self.retry_sleep * attempt
        if error is not None:
            retry_after = error.headers.get("Retry-After") if error.headers else None
            if retry_after and retry_after.isdigit():
                delay = min(float(retry_after), 5.0)
        time.sleep(delay)


class CTFdPlatformAdapter(PlatformAdapter):
    def __init__(
        self,
        url: str,
        token: str,
        *,
        profile_name: str = "default",
        team: str = "",
        flag_format: str = "",
        submit_enabled: bool = False,
        transport: CTFdTransport | None = None,
        retries: int = 3,
        timeout: int = 30,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.profile = CTFdProfile(
            name=profile_name,
            url=self.url,
            token=token,
            team=team,
            flag_format=flag_format,
            submit_enabled=submit_enabled,
            retries=retries,
            timeout=timeout,
        )
        self.transport = transport or UrllibCTFdTransport(base_url=self.url, token=token, retries=retries, timeout=timeout)

    @classmethod
    def from_profile(cls, profile: CTFdProfile, *, transport: CTFdTransport | None = None) -> "CTFdPlatformAdapter":
        return cls(
            profile.url,
            profile.token,
            profile_name=profile.name,
            team=profile.team,
            flag_format=profile.flag_format,
            submit_enabled=profile.submit_enabled,
            transport=transport,
            retries=profile.retries,
            timeout=profile.timeout,
        )

    def list_challenges(self) -> list[Challenge]:
        payload = self.transport.get_json("/api/v1/challenges")
        self._ensure_success(payload, "list challenges")
        return [self._challenge_from_listing(item) for item in payload.get("data", [])]

    def get_challenge(self, challenge_id: str) -> Challenge:
        payload = self.transport.get_json(f"/api/v1/challenges/{challenge_id}")
        self._ensure_success(payload, f"get challenge {challenge_id}")
        data = payload.get("data", payload)
        return self._challenge_from_detail(data)

    def download_files(self, challenge: Challenge, destination: str | Path) -> list[Artifact]:
        destination_path = Path(destination).expanduser()
        destination_path.mkdir(parents=True, exist_ok=True)
        artifacts: list[Artifact] = []
        local_files: list[str] = []
        downloads: list[dict[str, Any]] = []
        for file_url in challenge.files:
            url = self._absolute_url(file_url)
            name = _download_name(file_url)
            target = _unique_target(destination_path / name)
            info = self.transport.download(url, target)
            if info is None:
                info = _download_info(url, target)
            local_files.append(target.name)
            metadata = {
                "source": url,
                "original_url": file_url,
                "profile": self.profile.name,
                "sha256": info.sha256,
                "size": info.size,
            }
            downloads.append({"local_name": target.name, **metadata})
            artifacts.append(
                Artifact(
                    path=str(target),
                    kind="challenge-file",
                    description=f"Downloaded CTFd challenge file {target.name}",
                    metadata=metadata,
                )
            )
        if local_files:
            challenge.metadata["original_files"] = list(challenge.files)
            challenge.metadata["downloaded_files"] = downloads
            challenge.files = local_files
        return artifacts

    def submit_flag(self, challenge: Challenge, flag: str, *, submit: bool = False, confirm: str | None = None) -> SubmissionResult:
        expected = self.confirmation_string(challenge)
        if not submit:
            return SubmissionResult(
                challenge_id=challenge.id,
                flag=flag,
                submitted=False,
                accepted=None,
                message="dry-run: pass --submit and confirmation string to submit to CTFd",
                metadata={"profile": self.profile.redacted(), "confirmation_required": expected},
            )
        if not self.profile.submit_enabled:
            return SubmissionResult(
                challenge_id=challenge.id,
                flag=flag,
                submitted=False,
                accepted=None,
                message="blocked: profile submit_enabled is false",
                metadata={"profile": self.profile.redacted(), "confirmation_required": expected},
            )
        if confirm != expected:
            return SubmissionResult(
                challenge_id=challenge.id,
                flag=flag,
                submitted=False,
                accepted=None,
                message=f"blocked: confirmation string must exactly equal {expected!r}",
                metadata={"profile": self.profile.redacted(), "confirmation_required": expected},
            )

        payload = self.transport.post_json("/api/v1/challenges/attempt", {"challenge_id": int(challenge.id) if str(challenge.id).isdigit() else challenge.id, "submission": flag})
        self._ensure_success(payload, "submit flag", allow_incorrect=True)
        data = payload.get("data", {})
        status = data.get("status") or payload.get("status")
        accepted = str(status).lower() in {"correct", "true", "accepted"}
        return SubmissionResult(
            challenge_id=challenge.id,
            flag=flag,
            submitted=True,
            accepted=accepted,
            message=str(data.get("message") or payload.get("message") or status or ""),
            metadata={"response": payload, "profile": self.profile.redacted()},
        )

    def confirmation_string(self, challenge: Challenge) -> str:
        return f"SUBMIT {self.profile.name} {challenge.id}"

    def _challenge_from_listing(self, data: dict[str, Any]) -> Challenge:
        metadata = self._common_metadata(data)
        metadata["listing"] = True
        return Challenge(
            id=str(data["id"]),
            title=str(data.get("name") or data.get("title") or data["id"]),
            category=str(data.get("category") or "unknown"),
            flag_regex=self.profile.flag_format or None,
            metadata=metadata,
        )

    def _challenge_from_detail(self, data: dict[str, Any]) -> Challenge:
        files = [str(item) for item in data.get("files", [])]
        metadata = self._common_metadata(data)
        metadata["listing"] = False
        return Challenge(
            id=str(data["id"]),
            title=str(data.get("name") or data.get("title") or data["id"]),
            category=str(data.get("category") or "unknown"),
            description=str(data.get("description") or ""),
            files=files,
            connection=data.get("connection_info") or data.get("connection"),
            hints=[str(item.get("content", item)) for item in data.get("hints", [])],
            flag_regex=data.get("flag_regex") or self.profile.flag_format or None,
            metadata=metadata,
        )

    def _common_metadata(self, data: dict[str, Any]) -> dict[str, Any]:
        locked = bool(data.get("locked", False))
        hidden = bool(data.get("hidden", False) or data.get("state") == "hidden")
        value = data.get("value")
        solves = data.get("solves") or data.get("solve_count")
        return {
            "source": "ctfd",
            "profile": self.profile.name,
            "team": self.profile.team,
            "raw": data,
            "dynamic_scoring": {
                "type": data.get("type"),
                "value": value,
                "solves": solves,
                "solved_by_me": data.get("solved_by_me", data.get("solved")),
            },
            "locked": locked,
            "hidden": hidden,
            "available": not locked and not hidden,
        }

    def _absolute_url(self, file_url: str) -> str:
        return file_url if file_url.startswith(("http://", "https://")) else urllib.parse.urljoin(self.url + "/", file_url.lstrip("/"))

    def _ensure_success(self, payload: dict[str, Any], operation: str, *, allow_incorrect: bool = False) -> None:
        success = payload.get("success")
        if success is False:
            data = payload.get("data", {})
            status = str(data.get("status") or payload.get("status") or "").lower() if isinstance(data, dict) else ""
            if allow_incorrect and status in {"incorrect", "wrong"}:
                return
            raise CTFdAPIError(f"CTFd {operation} failed: {payload.get('message') or payload}")


def profile_from_config(config: dict[str, Any], profile_name: str | None = None, environ: dict[str, str] | None = None) -> CTFdProfile:
    environ = environ or os.environ
    ctfd = get_nested(config, ("platform", "ctfd")) or {}
    if not isinstance(ctfd, dict):
        raise CTFdConfigError("platform.ctfd must be a mapping")
    name = profile_name or str(ctfd.get("default_profile") or "default")
    profiles = ctfd.get("profiles", {})
    profile_data: dict[str, Any] = {}
    if isinstance(profiles, dict) and isinstance(profiles.get(name), dict):
        profile_data = dict(profiles[name])
    elif name == "default":
        profile_data = {key: ctfd.get(key) for key in ("url", "token", "team", "flag_format", "submit_enabled", "retries", "timeout") if key in ctfd}
    else:
        raise CTFdConfigError(f"CTFd profile not found: {name}")

    env_prefix = "CTF_AGENT_CTFD_" + _env_key(name) + "_"
    token = environ.get(env_prefix + "TOKEN") or environ.get("CTF_AGENT_CTFD_TOKEN") or profile_data.get("token") or ""
    url = environ.get(env_prefix + "URL") or profile_data.get("url") or ""
    if not url:
        raise CTFdConfigError(f"CTFd profile {name!r} requires url")
    return CTFdProfile(
        name=str(profile_data.get("name") or name),
        url=str(url).rstrip("/"),
        token=str(token or ""),
        team=str(environ.get(env_prefix + "TEAM") or profile_data.get("team") or ""),
        flag_format=str(profile_data.get("flag_format") or ""),
        submit_enabled=_as_bool(environ.get(env_prefix + "SUBMIT_ENABLED", profile_data.get("submit_enabled", False))),
        retries=int(profile_data.get("retries") or 3),
        timeout=int(profile_data.get("timeout") or 30),
    )


def adapter_from_config(config: dict[str, Any], profile_name: str | None = None, *, transport: CTFdTransport | None = None) -> CTFdPlatformAdapter:
    return CTFdPlatformAdapter.from_profile(profile_from_config(config, profile_name), transport=transport)


def _decode_json(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise CTFdAPIError("CTFd response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CTFdAPIError("CTFd response JSON must be an object")
    return payload


def _download_info(url: str, destination: Path) -> DownloadInfo:
    data = destination.read_bytes()
    return DownloadInfo(url=url, size=len(data), sha256=hashlib.sha256(data).hexdigest())


def _download_name(file_url: str) -> str:
    parsed = urllib.parse.urlparse(file_url)
    name = Path(urllib.parse.unquote(parsed.path)).name
    return name or "download.bin"


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not choose unique download path for {path}")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_key(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.upper())
