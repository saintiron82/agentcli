import json
import os
import sys

import pytest

from agentcli.launchers import prepare_subprocess_command, UnsafeLauncherError
from agentcli.providers.base import run_subprocess_sync


def test_native_arguments_are_not_changed():
    cmd = [sys.executable, "fake.py", "A&B|C%PATH%"]
    assert prepare_subprocess_command(cmd, windows=True) is cmd


@pytest.mark.parametrize("payload", ["SAFE&echo.AUDIT_MARKER", "a|b", "%PATH%", 'a"&b', "a\nb"])
def test_batch_wrapper_is_bypassed_and_argument_stays_one_value(tmp_path, payload):
    script = tmp_path / "fake.py"
    script.write_text("import sys,json; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
    executable = sys.executable
    if os.name != "nt":
        # Parser-only fixture; it is not executed on this platform.
        executable = str(tmp_path / "python.exe")
        (tmp_path / "python.exe").write_text("fake", encoding="utf-8")
    launcher = tmp_path / "fake.cmd"
    launcher.write_text(f'@echo off\n"{executable}" "{script}" %*\n', encoding="utf-8")
    original = [str(launcher), payload]
    resolved = prepare_subprocess_command(original, windows=True)
    assert resolved == [executable, str(script), payload]
    assert original == [str(launcher), payload]
    if os.name == "nt":
        stdout, _, rc, timed_out = run_subprocess_sync(original, timeout=5)
        assert rc == 0 and not timed_out
        assert json.loads(stdout) == [payload]


def test_unknown_batch_script_is_rejected_before_execution(tmp_path):
    launcher = tmp_path / "fake.cmd"
    launcher.write_text("@echo off\nrem opaque wrapper\n", encoding="utf-8")
    with pytest.raises(UnsafeLauncherError):
        prepare_subprocess_command([str(launcher), "SAFE&echo.AUDIT_MARKER"], windows=True)


def test_standard_npm_shim_resolves_to_node_and_entrypoint(tmp_path):
    entry = tmp_path / "node_modules/pkg/cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// fake", encoding="utf-8")
    node = tmp_path / "node.exe"
    node.write_text("fake", encoding="utf-8")
    shim = tmp_path / "tool.cmd"
    shim.write_text('@ECHO off\nSET "_prog=node"\n"%_prog%" "%dp0%\\node_modules\\pkg\\cli.js" %*\n', encoding="utf-8")
    assert prepare_subprocess_command([str(shim), "A&B"], windows=True) == [str(node), str(entry), "A&B"]
