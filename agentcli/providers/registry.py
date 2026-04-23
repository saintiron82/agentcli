"""프로바이더 레지스트리 — 등록, 검색, fallback 체인."""

from .base import LLMProvider


class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, LLMProvider] = {}
        self._fallback_order: list[str] = []

    def register(self, provider: LLMProvider) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> LLMProvider | None:
        return self._providers.get(provider_id)

    def list_providers(self) -> list[dict]:
        return [{"id": p.provider_id, "available": p.is_available(),
                 "supports_sessions": p.supports_sessions}
                for p in self._providers.values()]

    def list_models(self, provider_id: str = "") -> list[dict]:
        if provider_id:
            p = self.get(provider_id)
            return p.list_models() if p else []
        result = []
        for p in self._providers.values():
            for m in p.list_models():
                result.append({**m, "provider": p.provider_id})
        return result

    def set_fallback_order(self, order: list[str]) -> None:
        self._fallback_order = list(order)

    def get_fallback_chain(self) -> list[str]:
        return list(self._fallback_order)

    def get_next_fallback(self, current: str) -> str | None:
        try:
            idx = self._fallback_order.index(current)
            if idx + 1 < len(self._fallback_order):
                return self._fallback_order[idx + 1]
        except ValueError:
            pass
        return None


def create_default_registry() -> ProviderRegistry:
    from .claude import ClaudeProvider
    from .codex import CodexProvider
    from .copilot import CopilotProvider

    reg = ProviderRegistry()
    reg.register(ClaudeProvider())
    reg.register(CodexProvider())
    reg.register(CopilotProvider())
    # 세션 지원 provider를 우선 — 히스토리를 재주입 없이 CLI가 관리.
    # Codex --full-auto는 쓰기 권한 + 긴 응답으로 가장 비싸므로 후순위.
    reg.set_fallback_order(["claude", "copilot", "codex"])
    return reg
