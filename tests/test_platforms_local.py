from pathlib import Path

from ctf_agent.platforms.local import LocalPlatformAdapter


def write_challenge(root: Path, name: str = "challenge1") -> Path:
    challenge_dir = root / name
    challenge_dir.mkdir()
    (challenge_dir / "challenge.yaml").write_text(
        """
title: Local Test
category: misc
description: Adapter fixture
files:
  - prompt.txt
connection: nc ctf.example 31337
flag_regex: flag\\{.*\\}
""",
        encoding="utf-8",
    )
    (challenge_dir / "prompt.txt").write_text("flag{local_fixture}\n", encoding="utf-8")
    return challenge_dir


def test_local_adapter_lists_challenge_yaml_directories(tmp_path):
    write_challenge(tmp_path)
    adapter = LocalPlatformAdapter(tmp_path)
    challenges = adapter.list_challenges()
    assert len(challenges) == 1
    assert challenges[0].id == "challenge1"
    assert challenges[0].title == "Local Test"
    assert challenges[0].files == ["prompt.txt"]
    assert challenges[0].connection == "nc ctf.example 31337"


def test_local_adapter_gets_challenge_from_path(tmp_path):
    challenge_dir = write_challenge(tmp_path, "rev")
    challenge = LocalPlatformAdapter(tmp_path).get_challenge(str(challenge_dir))
    assert challenge.category == "misc"
    assert challenge.metadata["source"] == "local"


def test_local_adapter_infers_directory_without_yaml(tmp_path):
    challenge_dir = tmp_path / "forensics-basic"
    challenge_dir.mkdir()
    (challenge_dir / "capture.pcap").write_bytes(b"pcap")
    challenge = LocalPlatformAdapter(tmp_path).get_challenge("forensics-basic")
    assert challenge.title == "Forensics Basic"
    assert challenge.files == ["capture.pcap"]
    assert challenge.metadata["inferred"] is True


def test_local_adapter_download_files_copies_into_destination(tmp_path):
    write_challenge(tmp_path)
    adapter = LocalPlatformAdapter(tmp_path)
    challenge = adapter.get_challenge("challenge1")
    artifacts = adapter.download_files(challenge, tmp_path / "downloaded")
    assert len(artifacts) == 1
    assert Path(artifacts[0].path).read_text(encoding="utf-8") == "flag{local_fixture}\n"


def test_local_adapter_submit_is_always_dry_run(tmp_path):
    write_challenge(tmp_path)
    adapter = LocalPlatformAdapter(tmp_path)
    challenge = adapter.get_challenge("challenge1")
    result = adapter.submit_flag(challenge, "flag{local_fixture}", submit=True)
    assert result.submitted is False
    assert result.accepted is None
    assert result.metadata["requested_submit"] is True
