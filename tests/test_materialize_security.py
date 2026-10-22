import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agentcli import AgentProfile
from agentcli.profile import MANAGED_MARKER


def profile_with_skill(tmp_path):
    source = tmp_path / "source"
    skill = source / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("initial", encoding="utf-8")
    return AgentProfile("test", instructions="test", skills_dir=source)


@pytest.mark.parametrize("filename", ["../outside.md", "nested/file.md", "/absolute.md"])
def test_materialize_rejects_escape_before_writing(tmp_path, filename):
    target = tmp_path / "workspace"
    with pytest.raises(ValueError):
        AgentProfile("test", instructions="test").materialize(target, files=(filename,))
    assert not target.exists()
    assert not (tmp_path / "outside.md").exists()


def test_unmanaged_skill_is_preserved(tmp_path):
    profile = profile_with_skill(tmp_path)
    target = tmp_path / "workspace"
    existing = target / ".agents/skills/demo"
    existing.mkdir(parents=True)
    (existing / "custom.txt").write_text("user data", encoding="utf-8")
    result = profile.materialize(target)
    assert (existing / "custom.txt").read_text(encoding="utf-8") == "user data"
    assert str(existing.resolve()) in result["skipped"]
    assert result["skills_copied"] == []


def test_managed_skill_refresh_is_atomic_on_copy_failure(tmp_path):
    profile = profile_with_skill(tmp_path)
    target = tmp_path / "workspace"
    profile.materialize(target)
    (profile.skills_dir / "demo/SKILL.md").write_text("updated", encoding="utf-8")
    with patch("agentcli.profile.shutil.copytree", side_effect=OSError("fake disk failure")):
        with pytest.raises(OSError):
            profile.materialize(target)
    copied = target / ".agents/skills/demo/SKILL.md"
    assert copied.read_text(encoding="utf-8") == "initial"
    profile.materialize(target)
    assert copied.read_text(encoding="utf-8") == "updated"


def test_failed_replacement_and_failed_rollback_preserve_backup(tmp_path, monkeypatch):
    profile = profile_with_skill(tmp_path)
    target = tmp_path / "workspace"
    profile.materialize(target)
    original_replace = Path.replace
    def replace(path, destination):
        if path.name in ("new", "previous"):
            raise OSError("fake rename failure")
        return original_replace(path, destination)
    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(RuntimeError, match="preserved backup"):
        profile.materialize(target)
    backups = list((target / ".agents/skills").glob(".agentcli-skill-*/previous/SKILL.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "initial"


def test_marker_mentioned_in_user_content_does_not_allow_overwrite(tmp_path):
    original = "User instructions\nExample marker: " + MANAGED_MARKER
    (tmp_path / "AGENTS.md").write_text(original, encoding="utf-8")
    AgentProfile("test", instructions="new").materialize(tmp_path)
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == original


def test_junction_or_symlink_cannot_redirect_skill_writes(tmp_path):
    profile = profile_with_skill(tmp_path)
    target = tmp_path / "workspace"
    parent = target / ".agents"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = parent / "skills"
    if os.name == "nt":
        quoted_link = str(link).replace("'", "''")
        quoted_outside = str(outside).replace("'", "''")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"New-Item -ItemType Junction -Path '{quoted_link}' -Target '{quoted_outside}' | Out-Null"],
                       check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        profile.materialize(target)
    assert list(outside.iterdir()) == []
    assert not (target / "AGENTS.md").exists()


def test_source_inside_managed_destination_is_rejected_before_any_write(tmp_path):
    target = tmp_path / "workspace"
    existing = target / ".agents/skills/demo"
    source = existing / "profile/skills"
    skill = source / "demo"
    skill.mkdir(parents=True)
    (existing / ".agentcli-managed.json").write_text('{"managed_by":"agentcli"}')
    original = existing / "profile/AGENTS.md"
    original.write_text("canonical source", encoding="utf-8")
    (skill / "SKILL.md").write_text("source skill", encoding="utf-8")
    profile = AgentProfile("nested", instructions="new", skills_dir=source)
    with pytest.raises(ValueError, match="overlap"):
        profile.materialize(target)
    assert original.read_text(encoding="utf-8") == "canonical source"
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "source skill"
    assert not (target / "AGENTS.md").exists()
    assert not list(existing.parent.glob(".agentcli-skill-*"))


def test_root_ancestor_alias_is_allowed_but_child_links_are_rejected(tmp_path):
    from agentcli import AuthProfile
    from agentcli.filesystem import safe_workspace_path
    from agentcli.providers.kiro import KiroProvider

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    if os.name == "nt":
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "New-Item -ItemType Junction -Path '{}' -Target '{}' | Out-Null".format(
                            str(alias).replace("'", "''"), str(real).replace("'", "''"))],
                       check=True, capture_output=True)
    else:
        alias.symlink_to(real, target_is_directory=True)
    workspace = alias / "workspace"
    workspace.mkdir()
    child = workspace / "file.txt"
    child.write_text("data", encoding="utf-8")
    assert safe_workspace_path(workspace, child) == child.resolve()
    assert KiroProvider()._within_cwd(str(child), str(workspace)) == child.resolve()
    auth = AuthProfile("alias", str(alias / "profile"), claude_oauth_token="FAKE").prepare()
    assert auth.root == str((real / "profile").resolve())
    with pytest.raises(PermissionError):
        safe_workspace_path(workspace, workspace / "../outside.txt")
    with pytest.raises(PermissionError):
        safe_workspace_path(tmp_path, alias / "workspace/file.txt")
