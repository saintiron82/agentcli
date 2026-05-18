"""AgentProfile / AgentRegistry / 드리프트 옵저버 테스트."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from agentcli import (
    AgentProfile, AgentRegistry, LLMClient, MemoryStore,
    LLMResponse, TokenUsage, set_default_client,
)
from agentcli.client import DRIFT_KEY
from agentcli.profile import MANAGED_MARKER
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry


# ===== Fixtures =====

@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class RecordingProvider(LLMProvider):
    provider_id = "rec"
    supports_sessions = True

    def __init__(self):
        self.last_system_prompt = None
        self.last_cwd = None
        self.last_alias = None

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None, alias=""):
        self.last_cwd = cwd
        self.last_alias = alias
        self.last_system_prompt = next(
            (m.content for m in messages if m.role == "system"), None)
        return LLMResponse(
            content="ok", provider=self.provider_id, model=model,
            tokens=TokenUsage(total_tokens=5),
            session_id=session_id or f"sid-{alias or 'x'}")

    async def invoke_async(self, messages, *, model="", timeout=120,
                           session_id="", cwd=None, alias=""):
        return self.invoke(messages, model=model, timeout=timeout,
                           session_id=session_id, cwd=cwd, alias=alias)

    def list_models(self): return []
    def is_available(self): return True


def _make_client_with_rec():
    p = RecordingProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["rec"])
    return LLMClient(store=MemoryStore(), registry=reg), p


# ===== AgentProfile: 생성 / 해석 / 해시 =====

def test_profile_inline_instructions():
    p = AgentProfile(name="bull", instructions="너는 강세 분석가다")
    assert p.resolve_instructions() == "너는 강세 분석가다"
    assert len(p.hash()) == 16


def test_profile_from_dir_reads_agents_md(tmpdir_path):
    root = tmpdir_path / "bull-analyst"
    root.mkdir()
    (root / "AGENTS.md").write_text("강세 전문가", encoding="utf-8")
    (root / "profile.json").write_text(
        json.dumps({"model": "sonnet", "allowed_tools": ["Read", "Grep"]}),
        encoding="utf-8")

    p = AgentProfile.from_dir(root)
    assert p.name == "bull-analyst"
    assert p.resolve_instructions() == "강세 전문가"
    assert p.model == "sonnet"
    assert p.allowed_tools == ["Read", "Grep"]


def test_profile_from_dir_fallback_to_claude_md(tmpdir_path):
    root = tmpdir_path / "legacy"
    root.mkdir()
    (root / "CLAUDE.md").write_text("레거시 지시", encoding="utf-8")
    p = AgentProfile.from_dir(root)
    assert p.resolve_instructions() == "레거시 지시"


def test_profile_hash_stable(tmpdir_path):
    p1 = AgentProfile(name="x", instructions="abc", model="sonnet")
    p2 = AgentProfile(name="x", instructions="abc", model="sonnet")
    p3 = AgentProfile(name="x", instructions="abcd", model="sonnet")
    assert p1.hash() == p2.hash()
    assert p1.hash() != p3.hash()


# ===== Materialize: 안전 덮어쓰기 =====

def test_materialize_writes_agents_and_claude(tmpdir_path):
    p = AgentProfile(name="bull", instructions="강세")
    result = p.materialize(tmpdir_path)

    assert (tmpdir_path / "AGENTS.md").exists()
    assert (tmpdir_path / "CLAUDE.md").exists()
    assert MANAGED_MARKER in (tmpdir_path / "AGENTS.md").read_text()
    assert "강세" in (tmpdir_path / "AGENTS.md").read_text()
    assert result["hash"] == p.hash()


def test_materialize_does_not_overwrite_user_file(tmpdir_path):
    """마커 없는 기존 AGENTS.md는 건드리지 않고, override에 쓴다."""
    user_content = "사용자가 직접 쓴 AGENTS.md\n중요한 내용들"
    (tmpdir_path / "AGENTS.md").write_text(user_content, encoding="utf-8")

    p = AgentProfile(name="x", instructions="프로필 지시문")
    result = p.materialize(tmpdir_path)

    # AGENTS.md는 사용자 내용 유지
    assert (tmpdir_path / "AGENTS.md").read_text() == user_content
    # AGENTS.override.md에 프로필 내용 기록
    assert (tmpdir_path / "AGENTS.override.md").exists()
    assert "프로필 지시문" in (tmpdir_path / "AGENTS.override.md").read_text()
    assert any(str(tmpdir_path / "AGENTS.md") in s for s in result["skipped"])


def test_materialize_re_overwrites_marker_file(tmpdir_path):
    """마커가 있는 기존 파일은 덮어쓸 수 있다 (같은 라이브러리가 만든 것)."""
    p1 = AgentProfile(name="x", instructions="첫 버전")
    p1.materialize(tmpdir_path)
    v1 = (tmpdir_path / "AGENTS.md").read_text()
    assert "첫 버전" in v1

    p2 = AgentProfile(name="x", instructions="두 번째 버전")
    p2.materialize(tmpdir_path)
    v2 = (tmpdir_path / "AGENTS.md").read_text()
    assert "두 번째 버전" in v2
    assert "첫 버전" not in v2


def test_materialize_same_profile_produces_stable_instruction_file(tmpdir_path):
    p = AgentProfile(name="stable", instructions="같은 지시문")
    p.materialize(tmpdir_path)
    first = (tmpdir_path / "AGENTS.md").read_text(encoding="utf-8")

    p.materialize(tmpdir_path)
    second = (tmpdir_path / "AGENTS.md").read_text(encoding="utf-8")

    assert second == first


def test_materialize_copies_skills(tmpdir_path):
    """skills_dir → cwd/.agents/skills/"""
    profile_root = tmpdir_path / "profile-src"
    profile_root.mkdir()
    (profile_root / "AGENTS.md").write_text("지시문", encoding="utf-8")
    skills = profile_root / "skills"
    skills.mkdir()
    (skills / "my-skill").mkdir()
    (skills / "my-skill" / "SKILL.md").write_text("스킬 내용", encoding="utf-8")

    p = AgentProfile.from_dir(profile_root)
    target_cwd = tmpdir_path / "project"
    target_cwd.mkdir()
    result = p.materialize(target_cwd)

    dst_skill = target_cwd / ".agents" / "skills" / "my-skill" / "SKILL.md"
    assert dst_skill.exists()
    assert dst_skill.read_text() == "스킬 내용"
    assert len(result["skills_copied"]) == 1


# ===== AgentRegistry =====

def test_registry_from_dir(tmpdir_path):
    root = tmpdir_path / "profiles"
    root.mkdir()
    for n in ("bull", "bear", "trader"):
        d = root / n
        d.mkdir()
        (d / "AGENTS.md").write_text(f"{n} 지시문", encoding="utf-8")
    # 숨김 디렉토리는 무시됨
    (root / ".hidden").mkdir()
    (root / ".hidden" / "AGENTS.md").write_text("x", encoding="utf-8")

    reg = AgentRegistry.from_dir(root)
    assert len(reg) == 3
    assert reg.names() == ["bear", "bull", "trader"]
    assert reg.get("bull").resolve_instructions() == "bull 지시문"


def test_registry_failed_profile_warns_not_raises(tmpdir_path):
    root = tmpdir_path / "profiles"
    root.mkdir()
    ok = root / "good"
    ok.mkdir()
    (ok / "AGENTS.md").write_text("ok", encoding="utf-8")

    bad = root / "bad"
    bad.mkdir()
    (bad / "profile.json").write_text("not-json{", encoding="utf-8")

    # 기본 on_error='warn' — bad는 건너뛰고 good만 로드
    reg = AgentRegistry.from_dir(root)
    assert "good" in reg
    # bad는 profile.json 파싱 실패로 로드 실패
    assert "bad" not in reg


# ===== AgentProfile.chat 편의 호출 =====

def test_profile_chat_injects_instructions_and_alias(tmpdir_path):
    client, p = _make_client_with_rec()
    profile = AgentProfile(name="bull-analyst",
                           instructions="너는 강세 분석가다",
                           provider="rec", model="sonnet")
    resp = profile.chat("분석", client=client, owner="team")
    assert resp.content == "ok"
    assert p.last_alias == "bull-analyst"
    assert "강세 분석가" in (p.last_system_prompt or "")


def test_profile_chat_uses_default_cwd(tmpdir_path):
    client, p = _make_client_with_rec()
    profile = AgentProfile(name="x", instructions="지시",
                           provider="rec",
                           default_cwd=tmpdir_path.resolve())
    profile.chat("q", client=client, owner="u")
    assert Path(p.last_cwd).resolve() == tmpdir_path.resolve()


def test_profile_chat_async(tmpdir_path):
    client, p = _make_client_with_rec()
    profile = AgentProfile(name="x", instructions="지시", provider="rec")
    resp = asyncio.run(profile.chat_async("hi", client=client, owner="u"))
    assert resp.content == "ok"


def test_profile_materialize_before_call(tmpdir_path):
    """materialize=True 시 호출 전에 AGENTS.md 자재화."""
    client, p = _make_client_with_rec()
    profile = AgentProfile(name="bull", instructions="강세 지시", provider="rec")
    assert not (tmpdir_path / "AGENTS.md").exists()

    profile.chat("q", client=client, owner="u",
                 cwd=tmpdir_path, materialize=True)

    assert (tmpdir_path / "AGENTS.md").exists()
    assert "강세 지시" in (tmpdir_path / "AGENTS.md").read_text()


# ===== 드리프트 옵저버 =====

def test_drift_records_hash_in_metadata(tmpdir_path):
    client, _ = _make_client_with_rec()
    (tmpdir_path / "AGENTS.md").write_text("v1 지시문", encoding="utf-8")

    resp = client.chat("hi", provider="rec", owner="team",
                       alias="bull", cwd=str(tmpdir_path))

    conv = client._store.get(resp.conversation_id)
    hashes = conv.metadata.get(DRIFT_KEY)
    assert hashes is not None
    assert "AGENTS.md" in hashes


def test_drift_not_recorded_on_failed_new_call(tmpdir_path):
    class FailingProvider(RecordingProvider):
        provider_id = "failrec"

        def invoke(self, messages, *, model="", timeout=120, session_id="",
                   cwd=None, alias=""):
            return LLMResponse(content="", provider=self.provider_id, model=model)

    p = FailingProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["failrec"])
    client = LLMClient(store=store, registry=reg)
    (tmpdir_path / "AGENTS.md").write_text("v1 지시문", encoding="utf-8")

    resp = client.chat("hi", provider="failrec", owner="team",
                       alias="bull", cwd=str(tmpdir_path))

    assert resp.content == ""
    assert resp.conversation_id == ""
    assert store.list_by_owner("team") == []


def test_drift_detects_file_change(tmpdir_path, caplog):
    client, _ = _make_client_with_rec()
    (tmpdir_path / "AGENTS.md").write_text("원본", encoding="utf-8")

    client.chat("hi", provider="rec", owner="team", alias="x",
                cwd=str(tmpdir_path))
    # 파일 수정
    (tmpdir_path / "AGENTS.md").write_text("변경됨", encoding="utf-8")

    import logging
    with caplog.at_level(logging.WARNING):
        client.chat("again", provider="rec", owner="team", alias="x",
                    cwd=str(tmpdir_path))

    # 드리프트 로그 나왔는지
    drift_logs = [r for r in caplog.records if "드리프트" in r.message]
    assert len(drift_logs) >= 1


def test_drift_no_warning_when_unchanged(tmpdir_path, caplog):
    client, _ = _make_client_with_rec()
    (tmpdir_path / "AGENTS.md").write_text("동일", encoding="utf-8")

    client.chat("hi", provider="rec", owner="team", alias="x",
                cwd=str(tmpdir_path))

    import logging
    with caplog.at_level(logging.WARNING):
        client.chat("again", provider="rec", owner="team", alias="x",
                    cwd=str(tmpdir_path))

    drift_logs = [r for r in caplog.records if "드리프트" in r.message]
    assert len(drift_logs) == 0


def test_list_drifts_returns_rows(tmpdir_path):
    client, _ = _make_client_with_rec()
    (tmpdir_path / "AGENTS.md").write_text("x", encoding="utf-8")

    client.chat("hi", provider="rec", owner="team", alias="bull",
                cwd=str(tmpdir_path))

    rows = client.list_drifts(owner="team")
    assert len(rows) == 1
    assert rows[0]["alias"] == "bull"
    assert "AGENTS.md" in rows[0]["cwd_hashes"]


def test_get_alias_status_reports_fresh_and_stale(tmpdir_path):
    client, _ = _make_client_with_rec()
    agents = tmpdir_path / "AGENTS.md"
    agents.write_text("v1", encoding="utf-8")

    client.chat("hi", provider="rec", owner="team", alias="bull",
                cwd=str(tmpdir_path))

    fresh = client.get_alias_status("team", "bull", str(tmpdir_path))
    assert fresh["exists"] is True
    assert fresh["status"] == "fresh"
    assert fresh["fresh"] is True
    assert fresh["session_providers"] == ["rec"]

    agents.write_text("v2", encoding="utf-8")
    stat = agents.stat()
    os.utime(agents, (stat.st_atime, stat.st_mtime + 1))
    stale = client.get_alias_status("team", "bull", str(tmpdir_path))
    assert stale["status"] == "stale"
    assert stale["stale"] is True


def test_clear_session_metadata_blanks_agentcli_handles_only(tmpdir_path):
    client, _ = _make_client_with_rec()
    (tmpdir_path / "AGENTS.md").write_text("x", encoding="utf-8")

    resp = client.chat("hi", provider="rec", owner="team", alias="bull",
                       cwd=str(tmpdir_path), system_prompt="system v1")
    conv = client._store.get(resp.conversation_id)
    assert conv.metadata["session_id:rec"]
    assert conv.metadata["system_prompt_hash:rec"]
    assert conv.metadata[DRIFT_KEY]

    rows = client.clear_session_metadata(owner="team", alias="bull")

    assert rows[0]["conversation_id"] == resp.conversation_id
    conv = client._store.get(resp.conversation_id)
    assert conv.metadata["session_id:rec"] == ""
    assert conv.metadata["system_prompt_hash:rec"] == ""
    assert conv.metadata[DRIFT_KEY]
    status = client.get_alias_status("team", "bull", str(tmpdir_path))
    assert status["session_providers"] == []
