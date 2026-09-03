from pathlib import Path

import pytest
import urllib.error

from ctf_agent.core.models import Challenge
from ctf_agent.platforms.ctfd import CTFdPlatformAdapter, CTFdProfile, CTFdRateLimitError, DownloadInfo, UrllibCTFdTransport, adapter_from_config, profile_from_config


class FakeTransport:
    def __init__(self, *, submit_status="correct", rate_limit=False):
        self.posts = []
        self.downloads = []
        self.submit_status = submit_status
        self.rate_limit = rate_limit

    def get_json(self, path):
        if self.rate_limit:
            raise CTFdRateLimitError("rate limited")
        if path == "/api/v1/challenges":
            return {
                "success": True,
                "data": [
                    {"id": 7, "name": "Warmup", "category": "misc", "type": "dynamic", "value": 487, "solves": 13, "solved_by_me": False},
                    {"id": 8, "name": "Locked", "category": "pwn", "locked": True, "value": 500},
                ],
            }
        if path == "/api/v1/challenges/7":
            return {
                "success": True,
                "data": {
                    "id": 7,
                    "name": "Warmup",
                    "category": "misc",
                    "description": "demo",
                    "value": 487,
                    "solves": 13,
                    "files": ["/files/warmup.txt"],
                    "connection_info": "nc ctf.example 7",
                    "hints": [{"content": "look closer"}],
                },
            }
        raise AssertionError(path)

    def post_json(self, path, payload):
        self.posts.append((path, payload))
        return {"success": True, "data": {"status": self.submit_status, "message": self.submit_status}}

    def download(self, url, destination):
        self.downloads.append((url, destination))
        Path(destination).write_text("fixture", encoding="utf-8")
        return DownloadInfo(url=url, size=7, sha256="sha256-demo")


def test_ctfd_profile_from_config_supports_multiple_profiles_and_env():
    config = {
        "platform": {
            "ctfd": {
                "default_profile": "quals",
                "profiles": {
                    "quals": {
                        "url": "https://quals.example",
                        "token": "config-token",
                        "team": "blue",
                        "flag_format": r"flag\{[^}]+\}",
                        "submit_enabled": False,
                    },
                    "finals": {"url": "https://finals.example", "submit_enabled": True},
                },
            }
        }
    }
    profile = profile_from_config(config, environ={"CTF_AGENT_CTFD_QUALS_TOKEN": "env-token"})
    assert profile.name == "quals"
    assert profile.url == "https://quals.example"
    assert profile.token == "env-token"
    assert profile.submit_enabled is False
    assert profile.flag_format == r"flag\{[^}]+\}"
    assert profile_from_config(config, "finals").submit_enabled is True


def test_ctfd_adapter_lists_challenges_with_mock_transport():
    adapter = CTFdPlatformAdapter("https://ctf.example", "token", profile_name="demo", transport=FakeTransport())
    challenges = adapter.list_challenges()
    assert [challenge.id for challenge in challenges] == ["7", "8"]
    assert challenges[0].title == "Warmup"
    assert challenges[0].metadata["dynamic_scoring"]["type"] == "dynamic"
    assert challenges[0].metadata["dynamic_scoring"]["value"] == 487
    assert challenges[1].metadata["locked"] is True
    assert challenges[1].metadata["available"] is False


def test_ctfd_adapter_gets_detail_and_downloads_files(tmp_path):
    transport = FakeTransport()
    adapter = CTFdPlatformAdapter("https://ctf.example", "token", profile_name="demo", flag_format=r"flag\{.+\}", transport=transport)
    challenge = adapter.get_challenge("7")
    artifacts = adapter.download_files(challenge, tmp_path)
    assert challenge.connection == "nc ctf.example 7"
    assert challenge.hints == ["look closer"]
    assert challenge.flag_regex == r"flag\{.+\}"
    assert artifacts[0].path.endswith("warmup.txt")
    assert artifacts[0].metadata["original_url"] == "/files/warmup.txt"
    assert artifacts[0].metadata["source"] == "https://ctf.example/files/warmup.txt"
    assert artifacts[0].metadata["sha256"] == "sha256-demo"
    assert artifacts[0].metadata["size"] == 7
    assert challenge.files == ["warmup.txt"]
    assert challenge.metadata["original_files"] == ["/files/warmup.txt"]
    assert transport.downloads[0][0] == "https://ctf.example/files/warmup.txt"


def test_ctfd_submit_defaults_to_dry_run():
    transport = FakeTransport()
    adapter = CTFdPlatformAdapter("https://ctf.example", "token", profile_name="demo", submit_enabled=True, transport=transport)
    result = adapter.submit_flag(Challenge(id="7", title="Warmup", category="misc"), "flag{demo}")
    assert result.submitted is False
    assert "confirmation_required" in result.metadata
    assert transport.posts == []


def test_ctfd_submit_requires_profile_enabled():
    transport = FakeTransport()
    adapter = CTFdPlatformAdapter("https://ctf.example", "token", profile_name="demo", submit_enabled=False, transport=transport)
    result = adapter.submit_flag(Challenge(id="7", title="Warmup", category="misc"), "flag{demo}", submit=True, confirm="SUBMIT demo 7")
    assert result.submitted is False
    assert "submit_enabled is false" in result.message
    assert transport.posts == []


def test_ctfd_submit_requires_confirmation_string():
    transport = FakeTransport()
    adapter = CTFdPlatformAdapter("https://ctf.example", "token", profile_name="demo", submit_enabled=True, transport=transport)
    result = adapter.submit_flag(Challenge(id="7", title="Warmup", category="misc"), "flag{demo}", submit=True, confirm="yes")
    assert result.submitted is False
    assert "confirmation string" in result.message
    assert transport.posts == []


def test_ctfd_submit_correct_and_wrong_results():
    correct = CTFdPlatformAdapter("https://ctf.example", "token", profile_name="demo", submit_enabled=True, transport=FakeTransport(submit_status="correct"))
    challenge = Challenge(id="7", title="Warmup", category="misc")
    result = correct.submit_flag(challenge, "flag{demo}", submit=True, confirm="SUBMIT demo 7")
    assert result.submitted is True
    assert result.accepted is True

    wrong_transport = FakeTransport(submit_status="incorrect")
    wrong = CTFdPlatformAdapter("https://ctf.example", "token", profile_name="demo", submit_enabled=True, transport=wrong_transport)
    result = wrong.submit_flag(challenge, "flag{bad}", submit=True, confirm="SUBMIT demo 7")
    assert result.submitted is True
    assert result.accepted is False
    assert wrong_transport.posts == [("/api/v1/challenges/attempt", {"challenge_id": 7, "submission": "flag{bad}"})]


def test_ctfd_rate_limit_bubbles_from_transport():
    adapter = CTFdPlatformAdapter("https://ctf.example", "token", transport=FakeTransport(rate_limit=True))
    with pytest.raises(CTFdRateLimitError):
        adapter.list_challenges()


def test_adapter_from_config_uses_profile_transport():
    config = {"platform": {"ctfd": {"profiles": {"demo": {"url": "https://ctf.example", "token": "secret", "submit_enabled": True}}}}}
    adapter = adapter_from_config(config, "demo", transport=FakeTransport())
    assert adapter.profile.name == "demo"
    assert adapter.profile.token == "secret"
    assert adapter.profile.submit_enabled is True


class FakeHTTPResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


def test_urllib_transport_retries_rate_limit_then_succeeds(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many", {"Retry-After": "0"}, None)
        return FakeHTTPResponse(b'{"success": true, "data": []}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda delay: None)
    transport = UrllibCTFdTransport("https://ctf.example", "token", retries=2, timeout=9)
    assert transport.get_json("/api/v1/challenges") == {"success": True, "data": []}
    assert len(calls) == 2
    assert calls[0][1] == 9


def test_urllib_transport_retries_network_error_then_succeeds(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.URLError("temporary DNS failure")
        return FakeHTTPResponse(b'{"success": true, "data": [{"id": 1}]}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda delay: None)
    transport = UrllibCTFdTransport("https://ctf.example", "token", retries=2)
    assert transport.get_json("/api/v1/challenges")["data"][0]["id"] == 1
    assert len(calls) == 2
