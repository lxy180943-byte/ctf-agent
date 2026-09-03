import json

from ctf_agent.tools import RiskLevel, ToolRegistry, ToolSpec, build_tools_doctor, default_registry
from ctf_agent.tools.doctor import check_tool


def test_tool_spec_roundtrip():
    spec = ToolSpec(
        name="demo",
        category="generic",
        description="Demo tool",
        command_template="demo {path}",
        inputs=["path"],
        risk_level=RiskLevel.MEDIUM,
        required_bins=["demo"],
        install_hint="install demo",
    )
    restored = ToolSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
    assert restored == spec


def test_tool_registry_register_query_and_recommend():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="file", category="generic", description="Identify files", command_template="file {path}"))
    registry.register(ToolSpec(name="gdb", category="pwn", description="Debugger", command_template="gdb {path}"))

    assert registry.get("file").category == "generic"
    assert registry.query("debug")[0].name == "gdb"
    assert registry.recommend("pwn")[0].name == "gdb"
    assert registry.recommend("unknown")[0].name == "file"


def test_default_registry_contains_requested_categories_and_tools():
    registry = default_registry()
    names = {tool.name for tool in registry.list()}
    categories = set(registry.categories())
    assert {"generic", "pwn", "rev", "crypto", "web", "forensics"} <= categories
    assert {"file", "strings", "xxd", "hexdump", "rg"} <= names
    assert {"checksec", "gdb", "pwntools"} <= names
    assert {"readelf", "objdump", "radare2", "angr"} <= names
    assert {"python", "sage", "z3", "RsaCtfTool"} <= names
    assert {"curl", "nmap", "sqlmap", "ffuf", "playwright"} <= names
    assert {"binwalk", "exiftool", "foremost", "zsteg"} <= names


def test_tools_doctor_reports_missing_binary_without_crashing():
    registry = ToolRegistry(
        [
            ToolSpec(
                name="missing",
                category="generic",
                description="Missing fixture",
                command_template="missing",
                required_bins=["definitely-not-a-real-ctf-agent-bin"],
                install_hint="install missing",
            )
        ]
    )
    report = build_tools_doctor(registry)
    assert report["ok"] is True
    assert report["missing"] == 1
    assert report["checks"][0]["install_hint"] == "install missing"


def test_tools_doctor_reports_missing_python_package_without_crashing():
    registry = ToolRegistry(
        [
            ToolSpec(
                name="missing-package",
                category="rev",
                description="Missing package fixture",
                command_template="python -c 'import nope'",
                required_bins=["python"],
                install_hint="python -m pip install nope",
                metadata={"python_package": "definitely_not_a_real_python_ctf_agent_package"},
            )
        ]
    )
    report = build_tools_doctor(registry)
    assert report["missing"] == 1
    assert report["checks"][0]["missing_python_packages"] == ["definitely_not_a_real_python_ctf_agent_package"]


def test_check_tool_resolves_python_in_venv():
    check = check_tool(ToolSpec(name="python", category="crypto", description="", command_template="", required_bins=["python"]))
    assert check.available is True
    assert "python" in check.resolved_bins
