import os
from pathlib import Path

from ctf_agent.core import doctor


def test_command_version_args_for_compose():
    assert doctor.command_version_args("docker_compose") == ["docker", "compose", "version"]


def test_check_dirs_creates_expected_linux_home_paths(tmp_path):
    statuses = doctor.check_dirs(tmp_path, create=True)
    assert [Path(item.path).name for item in statuses] == list(doctor.DEFAULT_DIRS)
    assert all(item.exists for item in statuses)
    assert all(item.under_linux_home for item in statuses)


def test_wsl_detection_accepts_environment_marker(monkeypatch):
    monkeypatch.setitem(os.environ, "WSL_DISTRO_NAME", "UnitTestWSL")
    assert doctor.is_wsl() is True


def test_missing_optional_tool_includes_recommendation(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    status = doctor.check_tool("rg")
    assert status.ok is False
    assert "ripgrep" in (status.recommendation or "")



def test_llm_doctor_reports_openai_env(monkeypatch):
    monkeypatch.setenv('CTF_AGENT_LLM_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-openai-key')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-test')
    status = doctor.check_llm_config({'llm': {'provider': 'none'}})
    assert status.ok is True
    assert status.base_url == 'https://api.openai.com/v1'
    assert status.api_key_present is True


def test_llm_doctor_requires_openai_env_connection_fields(monkeypatch):
    monkeypatch.setenv('CTF_AGENT_LLM_PROVIDER', 'openai-compatible')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    status = doctor.check_llm_config({'llm': {'provider': 'none'}})
    assert status.ok is False
    assert status.api_key_present is False
    assert 'OPENAI_API_KEY' in (status.error or '')


def test_environment_report_contains_llm_section(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, 'check_tool', lambda name: doctor.ToolStatus(name=name, path=f'/usr/bin/{name}', ok=True, version='ok'))
    monkeypatch.setattr(doctor, 'is_wsl', lambda: True)
    report = doctor.build_report(create_dirs=True, docker_run=False, config={'llm': {'provider': 'none'}})
    assert 'llm' in report
    assert report['llm']['provider'] == 'none'
