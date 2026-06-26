"""AgentProfile / AgentRegistry 추가 커버리지 테스트.

기존 tests/test_profile.py 가 닿지 않는 분기(자재화 엣지, 지시문 해석 캐시/
에러, 기본 클라이언트 싱글톤, 레지스트리 add/get/list/에러)를 행위 기준으로 검증.
잔여(residual) 미커버 라인을 채우는 것이 목적.
"""

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import pytest

from agentcli import (
    AgentProfile, AgentRegistry, LLMClient, MemoryStore,
    LLMResponse, TokenUsage, set_default_client,
)
from agentcli.profile import (
    MANAGED_MARKER,
    _get_default_client,
)
import agentcli.profile as profile_mod
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry


# ===== Fixtures / 헬퍼 =====

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
        self.stream_called = False

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


@pytest.fixture
def reset_default_client():
    """기본 클라이언트 싱글톤을 테스트 후 원복 (전역 오염 방지)."""
    saved = profile_mod._default_client
    profile_mod._default_client = None
    try:
        yield
    finally:
        profile_mod._default_client = saved


# ===== from_dir: 에러 / mcp.json 분기 (line 80, 102-105) =====

def test_from_dir_missing_directory_raises(tmpdir_path):
    """존재하지 않는 디렉토리는 FileNotFoundError (line 80)."""
    missing = tmpdir_path / "nope"
    with pytest.raises(FileNotFoundError):
        AgentProfile.from_dir(missing)


def test_from_dir_path_is_file_raises(tmpdir_path):
    """디렉토리가 아닌 파일 경로도 FileNotFoundError (line 79-80)."""
    f = tmpdir_path / "afile.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        AgentProfile.from_dir(f)


def test_from_dir_reads_valid_mcp_json(tmpdir_path):
    """유효한 mcp.json 은 mcp_config 로 로드된다 (line 101-103)."""
    root = tmpdir_path / "with-mcp"
    root.mkdir()
    (root / "AGENTS.md").write_text("지시", encoding="utf-8")
    mcp = {"mcpServers": {"fs": {"command": "server-fs"}}}
    (root / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")

    p = AgentProfile.from_dir(root)
    assert p.mcp_config == mcp


def test_from_dir_malformed_mcp_json_warns_and_stays_empty(tmpdir_path, caplog):
    """깨진 mcp.json 은 경고만 남기고 mcp_config 는 빈 dict (line 104-105)."""
    root = tmpdir_path / "bad-mcp"
    root.mkdir()
    (root / "AGENTS.md").write_text("지시", encoding="utf-8")
    (root / "mcp.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        p = AgentProfile.from_dir(root)

    assert p.mcp_config == {}
    assert any("mcp.json" in r.message for r in caplog.records)


# ===== resolve_instructions: 캐시 히트 / OSError (line 141-148) =====

def test_resolve_instructions_uses_mtime_cache(tmpdir_path):
    """동일 mtime 이면 캐시된 텍스트를 반환한다 (line 141-142 캐시 히트)."""
    f = tmpdir_path / "AGENTS.md"
    f.write_text("처음 내용", encoding="utf-8")
    p = AgentProfile(name="x", instructions_file=f)

    first = p.resolve_instructions()
    assert first == "처음 내용"
    assert "_inst_cache" in p.__dict__

    # mtime 을 건드리지 않고 파일 본문만 바꿔치기 → 캐시가 우선되어 옛 내용 반환
    stat = f.stat()
    import os
    f.write_text("바뀐 내용", encoding="utf-8")
    os.utime(f, (stat.st_atime, stat.st_mtime))

    second = p.resolve_instructions()
    assert second == "처음 내용"  # 캐시 히트


def test_resolve_instructions_refreshes_on_mtime_change(tmpdir_path):
    """mtime 이 바뀌면 다시 읽는다 (line 143-145 재읽기)."""
    import os
    f = tmpdir_path / "AGENTS.md"
    f.write_text("v1", encoding="utf-8")
    p = AgentProfile(name="x", instructions_file=f)
    assert p.resolve_instructions() == "v1"

    stat = f.stat()
    f.write_text("v2", encoding="utf-8")
    os.utime(f, (stat.st_atime, stat.st_mtime + 5))
    assert p.resolve_instructions() == "v2"


def test_resolve_instructions_oserror_returns_empty(tmpdir_path, monkeypatch):
    """파일 stat/read 중 OSError 면 빈 문자열 (line 146-147)."""
    f = tmpdir_path / "AGENTS.md"
    f.write_text("내용", encoding="utf-8")
    p = AgentProfile(name="x", instructions_file=f)

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "stat", boom)
    assert p.resolve_instructions() == ""


def test_resolve_instructions_empty_when_no_source():
    """inline 도 file 도 없으면 빈 문자열 (line 148 폴백)."""
    p = AgentProfile(name="x")
    assert p.resolve_instructions() == ""


def test_resolve_instructions_missing_file_returns_empty(tmpdir_path):
    """파일 경로가 존재하지 않으면 빈 문자열 (line 137 False 분기)."""
    p = AgentProfile(name="x", instructions_file=tmpdir_path / "absent.md")
    assert p.resolve_instructions() == ""


# ===== materialize: 빈 지시문 경고 / skills dst 존재 (line 181, 209) =====

def test_materialize_empty_instructions_warns(tmpdir_path, caplog):
    """지시문이 비면 경고 로그를 남긴다 (line 180-181)."""
    p = AgentProfile(name="empty", instructions="")
    with caplog.at_level(logging.WARNING):
        result = p.materialize(tmpdir_path)

    assert any("지시문이 비어 있음" in r.message for r in caplog.records)
    # 경고는 남기되 파일은 여전히 쓴다 (마커만 있는 파일)
    assert (tmpdir_path / "AGENTS.md").exists()
    assert result["hash"] == p.hash()


def test_materialize_skills_overwrites_existing_dst(tmpdir_path):
    """대상 .agents/skills/<name> 가 이미 있으면 rmtree 후 재복사 (line 208-209)."""
    profile_root = tmpdir_path / "src"
    profile_root.mkdir()
    (profile_root / "AGENTS.md").write_text("지시", encoding="utf-8")
    skills = profile_root / "skills"
    skills.mkdir()
    (skills / "my-skill").mkdir()
    (skills / "my-skill" / "SKILL.md").write_text("새 버전", encoding="utf-8")

    p = AgentProfile.from_dir(profile_root)
    target_cwd = tmpdir_path / "proj"
    target_cwd.mkdir()

    # 미리 stale 콘텐츠가 있는 대상 스킬 디렉토리를 만들어 둔다
    stale = target_cwd / ".agents" / "skills" / "my-skill"
    stale.mkdir(parents=True)
    (stale / "OLD.md").write_text("옛날 잔여 파일", encoding="utf-8")

    result = p.materialize(target_cwd)

    dst = target_cwd / ".agents" / "skills" / "my-skill"
    assert (dst / "SKILL.md").read_text() == "새 버전"
    # 잔여 파일은 rmtree 로 제거되어야 한다
    assert not (dst / "OLD.md").exists()
    assert len(result["skills_copied"]) == 1


def test_materialize_skips_skills_when_include_false(tmpdir_path):
    """include_skills=False 면 스킬 복사를 건너뛴다."""
    profile_root = tmpdir_path / "src"
    profile_root.mkdir()
    (profile_root / "AGENTS.md").write_text("지시", encoding="utf-8")
    skills = profile_root / "skills"
    skills.mkdir()
    (skills / "s1").mkdir()
    (skills / "s1" / "SKILL.md").write_text("내용", encoding="utf-8")

    p = AgentProfile.from_dir(profile_root)
    target_cwd = tmpdir_path / "proj"
    target_cwd.mkdir()
    result = p.materialize(target_cwd, include_skills=False)

    assert result["skills_copied"] == []
    assert not (target_cwd / ".agents").exists()


# ===== _can_write: OSError 읽기 실패 (line 240-241) =====

def test_can_write_returns_false_on_read_oserror(tmpdir_path, monkeypatch):
    """기존 파일 읽기가 OSError 면 안전하지 않다고 판단 (line 240-241)."""
    target = tmpdir_path / "AGENTS.md"
    target.write_text("뭔가 있음", encoding="utf-8")

    orig_read = Path.read_text

    def maybe_boom(self, *a, **k):
        if self == target:
            raise OSError("permission denied")
        return orig_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", maybe_boom)
    assert AgentProfile._can_write(target) is False


def test_can_write_true_for_legacy_marker(tmpdir_path):
    """레거시 마커가 있는 파일도 덮어쓰기 허용 (line 244)."""
    from agentcli.profile import _LEGACY_MARKERS
    target = tmpdir_path / "AGENTS.md"
    target.write_text(_LEGACY_MARKERS[0] + "\n옛 내용", encoding="utf-8")
    assert AgentProfile._can_write(target) is True


def test_can_write_false_for_plain_user_file(tmpdir_path):
    """마커 없는 사용자 파일은 덮어쓰기 불가 (line 242-244 모두 False)."""
    target = tmpdir_path / "AGENTS.md"
    target.write_text("순수 사용자 파일", encoding="utf-8")
    assert AgentProfile._can_write(target) is False


# ===== 기본 클라이언트 싱글톤 (line 285-286, 300-301, 333-338, 344) =====

def test_get_default_client_lazy_singleton(reset_default_client):
    """_get_default_client 는 지연 로드되어 같은 인스턴스를 재사용 (line 333-338)."""
    assert profile_mod._default_client is None
    c1 = _get_default_client()
    c2 = _get_default_client()
    assert c1 is c2
    assert isinstance(c1, LLMClient)


def test_set_default_client_overrides_singleton(reset_default_client):
    """set_default_client 로 외부 주입 시 그 인스턴스를 반환 (line 344)."""
    injected, _ = _make_client_with_rec()
    set_default_client(injected)
    assert _get_default_client() is injected


def test_chat_uses_default_client_when_none(reset_default_client, tmpdir_path):
    """client=None 이면 기본 싱글톤을 쓴다 (line 300-301)."""
    injected, prov = _make_client_with_rec()
    set_default_client(injected)
    profile = AgentProfile(name="x", instructions="지시", provider="rec")
    resp = profile.chat("질문", owner="u")
    assert resp.content == "ok"
    assert prov.last_alias == "x"


def test_chat_async_uses_default_client_when_none(reset_default_client):
    """chat_async 도 client=None 이면 기본 싱글톤 사용 (line 285-286)."""
    injected, prov = _make_client_with_rec()
    set_default_client(injected)
    profile = AgentProfile(name="y", instructions="지시", provider="rec")
    resp = asyncio.run(profile.chat_async("hi", owner="u"))
    assert resp.content == "ok"
    assert prov.last_alias == "y"


def test_chat_async_materializes_when_cwd_present(tmpdir_path):
    """chat_async + materialize=True + cwd 면 자재화 수행 (line 289-290)."""
    client, prov = _make_client_with_rec()
    profile = AgentProfile(name="bull", instructions="강세 지시", provider="rec")
    assert not (tmpdir_path / "AGENTS.md").exists()

    resp = asyncio.run(profile.chat_async(
        "q", client=client, owner="u", cwd=tmpdir_path, materialize=True))

    assert resp.content == "ok"
    assert (tmpdir_path / "AGENTS.md").exists()
    assert "강세 지시" in (tmpdir_path / "AGENTS.md").read_text()


# ===== chat_stream (line 315-322) =====

def test_chat_stream_yields_chunks(tmpdir_path):
    """chat_stream 이 클라이언트 스트림 청크를 그대로 흘려보낸다 (line 317-322)."""
    from agentcli.types import StreamChunk

    class StreamProvider(RecordingProvider):
        provider_id = "streamrec"
        supports_streaming = True

        async def stream_async(self, messages, *, model="", timeout=120,
                               session_id="", cwd=None, alias=""):
            self.last_alias = alias
            self.last_system_prompt = next(
                (m.content for m in messages if m.role == "system"), None)
            yield StreamChunk(type="text", content="part1")
            yield StreamChunk(type="text", content="part2")
            yield StreamChunk(type="done", content="part1part2",
                              session_id=session_id or f"sid-{alias or 'x'}")

    prov = StreamProvider()
    reg = ProviderRegistry()
    reg.register(prov)
    reg.set_fallback_order(["streamrec"])
    client = LLMClient(store=MemoryStore(), registry=reg)

    profile = AgentProfile(name="streamer", instructions="스트림 지시",
                           provider="streamrec")

    async def collect():
        out = []
        async for chunk in profile.chat_stream("q", client=client, owner="u"):
            out.append(chunk)
        return out

    chunks = asyncio.run(collect())
    texts = [c.content for c in chunks if c.type == "text"]
    assert "part1" in texts and "part2" in texts
    assert prov.last_alias == "streamer"
    assert "스트림 지시" in (prov.last_system_prompt or "")


def test_chat_stream_uses_default_client(reset_default_client):
    """chat_stream 도 client=None 이면 기본 싱글톤을 쓴다 (line 315-316)."""
    from agentcli.types import StreamChunk

    class StreamProvider(RecordingProvider):
        provider_id = "streamrec2"
        supports_streaming = True

        async def stream_async(self, messages, *, model="", timeout=120,
                               session_id="", cwd=None, alias=""):
            yield StreamChunk(type="text", content="x")
            yield StreamChunk(type="done", content="x",
                              session_id=f"sid-{alias or 'x'}")

    prov = StreamProvider()
    reg = ProviderRegistry()
    reg.register(prov)
    reg.set_fallback_order(["streamrec2"])
    injected = LLMClient(store=MemoryStore(), registry=reg)
    set_default_client(injected)

    profile = AgentProfile(name="s", instructions="지시", provider="streamrec2")

    async def collect():
        return [c async for c in profile.chat_stream("q", owner="u")]

    chunks = asyncio.run(collect())
    assert any(c.type == "text" for c in chunks)


def test_chat_stream_materializes(tmpdir_path):
    """chat_stream + materialize=True + cwd 면 자재화 (line 319-320)."""
    from agentcli.types import StreamChunk

    class StreamProvider(RecordingProvider):
        provider_id = "streamrec3"
        supports_streaming = True

        async def stream_async(self, messages, *, model="", timeout=120,
                               session_id="", cwd=None, alias=""):
            yield StreamChunk(type="text", content="done-part")
            yield StreamChunk(type="done", content="done-part",
                              session_id=f"sid-{alias or 'x'}")

    prov = StreamProvider()
    reg = ProviderRegistry()
    reg.register(prov)
    reg.set_fallback_order(["streamrec3"])
    client = LLMClient(store=MemoryStore(), registry=reg)

    profile = AgentProfile(name="mat", instructions="자재화 지시",
                           provider="streamrec3")

    async def drain():
        async for _ in profile.chat_stream(
                "q", client=client, owner="u",
                cwd=tmpdir_path, materialize=True):
            pass

    asyncio.run(drain())
    assert (tmpdir_path / "AGENTS.md").exists()
    assert "자재화 지시" in (tmpdir_path / "AGENTS.md").read_text()


# ===== AgentRegistry: register/get/list/contains (line 362) =====

def test_registry_register_get_and_duplicate():
    """register/get; 같은 이름 재등록은 마지막 것으로 덮어쓴다."""
    reg = AgentRegistry()
    p1 = AgentProfile(name="dup", instructions="첫 번째")
    p2 = AgentProfile(name="dup", instructions="두 번째")

    reg.register(p1)
    assert reg.get("dup") is p1
    assert "dup" in reg
    assert len(reg) == 1

    reg.register(p2)  # 동일 이름 → 덮어쓰기
    assert reg.get("dup") is p2
    assert len(reg) == 1


def test_registry_get_missing_returns_none():
    """없는 이름 조회는 None."""
    reg = AgentRegistry()
    assert reg.get("nope") is None
    assert "nope" not in reg


def test_registry_list_sorted_by_name():
    """list() 는 이름순 정렬된 프로필 객체를 돌려준다 (line 362)."""
    reg = AgentRegistry()
    reg.register(AgentProfile(name="zeta"))
    reg.register(AgentProfile(name="alpha"))
    reg.register(AgentProfile(name="mid"))

    listed = reg.list()
    assert [p.name for p in listed] == ["alpha", "mid", "zeta"]
    assert reg.names() == ["alpha", "mid", "zeta"]


# ===== AgentRegistry.from_dir: 에러 분기 (line 384, 394) =====

def test_registry_from_dir_missing_root_raises(tmpdir_path):
    """루트가 디렉토리가 아니면 FileNotFoundError (line 383-384)."""
    with pytest.raises(FileNotFoundError):
        AgentRegistry.from_dir(tmpdir_path / "no-such-root")


def test_registry_from_dir_on_error_raise_propagates(tmpdir_path):
    """on_error='raise' 면 깨진 프로필에서 예외를 다시 던진다 (line 393-394)."""
    root = tmpdir_path / "profiles"
    root.mkdir()
    bad = root / "bad"
    bad.mkdir()
    (bad / "profile.json").write_text("not-json{", encoding="utf-8")

    with pytest.raises(Exception):
        AgentRegistry.from_dir(root, on_error="raise")


def test_registry_from_dir_on_error_ignore_swallows(tmpdir_path, caplog):
    """on_error='ignore' 면 경고도 없이 조용히 건너뛴다."""
    root = tmpdir_path / "profiles"
    root.mkdir()
    good = root / "good"
    good.mkdir()
    (good / "AGENTS.md").write_text("ok", encoding="utf-8")
    bad = root / "bad"
    bad.mkdir()
    (bad / "profile.json").write_text("broken{", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        reg = AgentRegistry.from_dir(root, on_error="ignore")

    assert "good" in reg
    assert "bad" not in reg
    # ignore 모드는 경고 로그를 남기지 않는다
    assert not any("프로필 로드 실패" in r.message for r in caplog.records)


# ===== materialize_all (line 406-413) =====

def test_materialize_all_writes_each_profile(tmpdir_path):
    """전체 프로필을 한 cwd 에 일괄 자재화 (line 406-413)."""
    reg = AgentRegistry()
    reg.register(AgentProfile(name="a", instructions="A 지시"))
    reg.register(AgentProfile(name="b", instructions="B 지시"))

    target = tmpdir_path / "cwd"
    target.mkdir()
    results = reg.materialize_all(target)

    assert len(results) == 2
    # 같은 cwd 라 AGENTS.md 는 마지막(이름순 정렬) 프로필 'b' 로 덮인다
    assert "B 지시" in (target / "AGENTS.md").read_text()
    names_in_results = {r["name"] for r in results}
    assert names_in_results == {"a", "b"}


def test_materialize_all_with_names_filter(tmpdir_path):
    """names 인자로 일부만 자재화 (line 406 분기)."""
    reg = AgentRegistry()
    reg.register(AgentProfile(name="a", instructions="A"))
    reg.register(AgentProfile(name="b", instructions="B"))

    target = tmpdir_path / "cwd"
    target.mkdir()
    results = reg.materialize_all(target, names=["a"])

    assert len(results) == 1
    assert results[0]["name"] == "a"


def test_materialize_all_skips_unknown_name(tmpdir_path):
    """존재하지 않는 이름은 조용히 건너뛴다 (line 410-411 None 분기)."""
    reg = AgentRegistry()
    reg.register(AgentProfile(name="a", instructions="A"))

    target = tmpdir_path / "cwd"
    target.mkdir()
    results = reg.materialize_all(target, names=["a", "ghost"])

    assert len(results) == 1
    assert results[0]["name"] == "a"
