import os
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell")

SCRIPT = Path(__file__).parents[1] / "src" / "win_offline_update" / "winre_setup.ps1"
MACHINE = "12345678-1234-1234-1234-123456789abc"


def run_setup_helper(
    command: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    # Load the helper definitions without running the installer.
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            """
$ErrorActionPreference = 'Stop'
$tokens = $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:WUO_SETUP_TEST_SCRIPT, [ref]$tokens, [ref]$errors
)
if ($errors.Count) { throw ($errors -join [Environment]::NewLine) }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst] -or
        $statement -is [System.Management.Automation.Language.AssignmentStatementAst]) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
"""
            + command,
        ],
        env={
            **os.environ,
            "WUO_SETUP_TEST_SCRIPT": str(SCRIPT),
            **environment,
        },
        check=False,
        capture_output=True,
        text=True,
    )


def edit_tools(
    image: Path, host: Path, action: str, *, allow_missing: bool = False
) -> subprocess.CompletedProcess[str]:
    return run_setup_helper(
        """
Edit-RecoveryTools -Mount $env:WUO_SETUP_TEST_IMAGE -HostSystem32 $env:WUO_SETUP_TEST_HOST `
    -Action $env:WUO_SETUP_TEST_ACTION -MachineGuid $env:WUO_SETUP_TEST_MACHINE `
    -LauncherText "@echo off`r`n" -AllowMissing:($env:WUO_SETUP_ALLOW_MISSING -eq '1')
""",
        {
            "WUO_SETUP_TEST_IMAGE": str(image),
            "WUO_SETUP_TEST_HOST": str(host),
            "WUO_SETUP_TEST_ACTION": action,
            "WUO_SETUP_TEST_MACHINE": MACHINE,
            "WUO_SETUP_ALLOW_MISSING": str(int(allow_missing)),
        },
    )


def test_install_and_remove_tool_preserves_other_recovery_files(tmp_path: Path) -> None:
    image = tmp_path / "WinRE"
    system32 = image / "Windows/System32"
    host = tmp_path / "Windows/System32"
    tools = image / "Sources/Recovery/Tools"
    tools.mkdir(parents=True)
    (tools / "vendor-notes.txt").write_text("keep", encoding="ascii")
    (system32 / "en-US").mkdir(parents=True)
    (host / "de-DE").mkdir(parents=True)
    (system32 / "cmd.exe").write_bytes(b"recovery cmd")
    (system32 / "en-US/cmd.exe.mui").write_bytes(b"recovery cmd resources")
    (host / "certutil.exe").write_bytes(b"installed certutil")
    (host / "findstr.exe").write_bytes(b"installed findstr")
    (host / "de-DE/findstr.exe.mui").write_bytes(b"installed findstr resources")

    install = edit_tools(image, host, "install")
    assert install.returncode == 0, install.stdout + install.stderr
    config = ElementTree.parse(tools / "WinREConfig.xml")
    assert config.findtext("./RecoveryTools/RelativeFilePath") == r"WUO\cmd.exe"
    assert config.findtext("./RecoveryTools/CommandLineParam") == (
        r"/d /c X:\Sources\Recovery\Tools\WUO\launcher.cmd"
    )
    payload = tools / "WUO"
    assert (payload / "owner.id").read_text(encoding="ascii").strip() == MACHINE
    assert (payload / "launcher.cmd").read_bytes() == b"@echo off\r\n"
    assert (payload / "cmd.exe").read_bytes() == b"recovery cmd"
    assert (payload / "en-US/cmd.exe.mui").read_bytes() == b"recovery cmd resources"
    assert (payload / "certutil.exe").read_bytes() == b"installed certutil"
    assert (payload / "findstr.exe").read_bytes() == b"installed findstr"
    assert (
        payload / "de-DE/findstr.exe.mui"
    ).read_bytes() == b"installed findstr resources"

    remove = edit_tools(image, host, "uninstall")
    assert remove.returncode == 0, remove.stdout + remove.stderr
    assert not payload.exists()
    assert not (tools / "WinREConfig.xml").exists()
    assert (tools / "vendor-notes.txt").read_text(encoding="ascii") == "keep"


@pytest.mark.parametrize(
    ("action", "allow_missing"),
    [("install", False), ("uninstall", False), ("uninstall", True)],
)
def test_foreign_recovery_tool_is_left_untouched(
    tmp_path: Path, action: str, allow_missing: bool
) -> None:
    tools = tmp_path / "WinRE/Sources/Recovery/Tools"
    tools.mkdir(parents=True)
    config = tools / "WinREConfig.xml"
    original = (
        b"<Recovery><RecoveryTools><RelativeFilePath>OEM.exe</RelativeFilePath>"
        b"</RecoveryTools></Recovery>"
    )
    config.write_bytes(original)
    result = edit_tools(
        tmp_path / "WinRE", tmp_path / "Host", action, allow_missing=allow_missing
    )
    assert result.returncode != 0
    assert "custom recovery tool" in result.stdout + result.stderr
    assert config.read_bytes() == original


def test_serviced_image_without_any_registered_tool_is_left_as_is(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "WinRE/Sources/Recovery/Tools"
    tools.mkdir(parents=True)
    (tools / "vendor-notes.txt").write_text("keep", encoding="ascii")

    remove = edit_tools(
        tmp_path / "WinRE", tmp_path / "Host", "uninstall", allow_missing=True
    )
    assert remove.returncode == 0, remove.stdout + remove.stderr
    assert remove.stdout.strip() == "False"
    assert sorted(path.name for path in tools.iterdir()) == ["vendor-notes.txt"]


def test_clear_owned_shell_label_preserves_other_recovery_settings(
    tmp_path: Path,
) -> None:
    windows = tmp_path / "Windows"
    recovery = windows / "System32/Recovery"
    recovery.mkdir(parents=True)
    active = tmp_path / "active"
    active.mkdir()
    agent = recovery / "ReAgent.xml"
    agent.write_text(
        '<WindowsRE version="2.0"><InstallState state="0"/><OemTool state="1"/>'
        '<WinreLocation path="original"/></WindowsRE>',
        encoding="ascii",
    )
    for path in (active, recovery):
        (path / "ReCustomization.xml").write_text(
            '<BootShell><WinRETool locale="en-us"><Name>Windows Offline Update</Name>'
            "<Description>Apply a staged update job</Description></WinRETool></BootShell>",
            encoding="ascii",
        )
    result = run_setup_helper(
        r"""
$system32 = Join-Path $env:WUO_SETUP_TEST_WINDOWS 'System32'
$agentConfig = Join-Path $system32 'Recovery\ReAgent.xml'
Clear-OwnedBootShell -RecoveryDirectory $env:WUO_SETUP_TEST_ACTIVE -Workspace $env:WUO_SETUP_TEST_WORK
""",
        {
            "WUO_SETUP_TEST_WINDOWS": str(windows),
            "WUO_SETUP_TEST_ACTIVE": str(active),
            "WUO_SETUP_TEST_WORK": str(tmp_path),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (active / "ReCustomization.xml").exists()
    assert not (recovery / "ReCustomization.xml").exists()
    state = ElementTree.parse(agent)
    oem = state.find("./OemTool")
    location = state.find("./WinreLocation")
    assert oem is not None and oem.get("state") == "0"
    assert location is not None and location.get("path") == "original"
