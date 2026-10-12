"""Pinned host context, native-session refinement and independent forks."""
import asyncio
import itertools
import uuid
from typing import TYPE_CHECKING
from .async_utils import complete_in_thread

if TYPE_CHECKING:
    from .client import LLMClient


class ContextSession:
    """큰 컨텍스트를 1회 주입하고 그 위에서 여러 질의를 던지는 핸들.

    ``LLMClient.pin_context(...)`` 가 생성. 두 가지 방식:

    - ``refine(prompt)`` — **이어가기**: 같은 세션을 resume 하여 이전 답을 본다.
      세션이 살아있으면 전사록을 재전송하지 않고 지시만 보낸다(효율). 시간이
      지났거나/프로세스 재시작/세션 만료로 세션이 죽었으면 ``session_alive`` 로
      판정해 **자동으로 전사록을 재시드**한다 — "잠시 있다가 다시 요청" 대응.
    - ``fork(prompt)`` — **독립 변형**: 매번 전사록을 새 세션에 재시드하여 변형
      끼리 섞이지 않는다(예: 같은 전사록으로 격식 회의록 / 액션아이템 / 캐주얼
      요약을 따로). 효율은 provider 의 프롬프트 캐싱에 의존.

    라이브러리는 세션 id 만 저장하므로(설계 불변식), 전사록 본문은 호스트(이
    객체)가 들고 있다. 나중에 같은 ``alias`` 로 다시 ``pin_context`` 하면 살아
    있으면 resume, 죽었으면 재시드한다.

    각 메서드는 sync/async/stream 3종: ``refine``/``refine_async``/
    ``refine_stream``, ``fork``/``fork_async``/``fork_stream``. 순차 사용 전제.
    """

    def __init__(self, client: "LLMClient", context: str, *,
                 provider: str = "", owner: str = "", alias: str = "",
                 cwd: str | None = None, model: str = "",
                 separator: str = "\n\n---\n", **chat_kwargs):
        if not context:
            raise ValueError("context must be a non-empty string")
        self._client = client
        self._context = context
        self._provider = provider or next(iter(client._registry.get_fallback_chain()), "")
        self._owner = owner
        self._alias = alias or f"ctx-{uuid.uuid4().hex[:8]}"
        self._cwd = cwd
        self._model = model
        self._sep = separator
        self._chat_kwargs = chat_kwargs
        self._seeded = False
        # itertools.count().__next__ 는 GIL/이벤트루프 인터리빙 모두에서 원자적
        # (읽기-수정-쓰기 3단계 아님) — 동기 fork()/향후 스레드 유입 대비 하드닝.
        self._fork_n = itertools.count(1)

    @property
    def alias(self) -> str:
        return self._alias

    def is_alive(self) -> bool | None:
        """pin 된 세션이 현재 재개 가능한지 (LLM 호출 없이)."""
        return self._client.session_alive(
            self._provider, owner=self._owner, alias=self._alias, cwd=self._cwd)

    def _seed_prompt(self, prompt: str) -> str:
        return f"{self._context}{self._sep}{prompt}"

    def _opts(self, alias: str, kw: dict) -> dict:
        opts = dict(provider=self._provider, owner=self._owner, alias=alias,
                    cwd=self._cwd, model=self._model)
        opts.update(self._chat_kwargs)
        opts.update(kw)
        return opts

    def _refine(self, prompt: str) -> tuple[str, dict]:
        """세션 생존 여부로 전사록 재시드 결정. ``(prompt, extra_kwargs)`` 반환.

        살아있음(True) → 지시만(전사록 이미 세션에 있음), resume. 죽음/없음(False)
        → 전사록 재시드 + ``new_session=True``(죽은 sid 에 의존하지 않고 새 세션을
        명시적으로 시작). 판단불가(None, 예: copilot) → 이 프로세스에서 시드한 적
        있으면 신뢰(resume), 아니면 안전하게 재시드.
        """
        alive = self.is_alive()
        need_seed = (alive is False) or (alive is None and not self._seeded)
        self._seeded = True
        if need_seed:
            return self._seed_prompt(prompt), {"new_session": True}
        return prompt, {}

    # ---- refine: 이어가기 (resume; 죽었으면 새 세션에 재시드) ----
    def refine(self, prompt: str, **kw):
        p, extra = self._refine(prompt)
        return self._client.chat(p, **self._opts(self._alias, {**extra, **kw}))

    async def refine_async(self, prompt: str, **kw):
        p, extra = await complete_in_thread(self._refine, prompt)
        return await self._client.chat_async(
            p, **self._opts(self._alias, {**extra, **kw}))

    def refine_stream(self, prompt: str, **kw):
        return self._refine_stream(prompt, kw)

    async def _refine_stream(self, prompt: str, kw: dict):
        p, extra = await complete_in_thread(self._refine, prompt)
        stream = self._client.chat_stream(p, **self._opts(self._alias, {**extra, **kw}))
        try:
            async for chunk in stream:
                yield chunk
        finally:
            await stream.aclose()

    # ---- fork: 독립 변형 (매번 새 세션에 전사록 재시드) ----
    def _fork_target(self, prompt: str, label: str) -> tuple[str, str]:
        n = next(self._fork_n)
        return (self._seed_prompt(prompt),
                f"{self._alias}#fork:{label or n}")

    def fork(self, prompt: str, *, label: str = "", **kw):
        p, a = self._fork_target(prompt, label)
        return self._client.chat(p, new_session=True, **self._opts(a, kw))

    async def fork_async(self, prompt: str, *, label: str = "", **kw):
        p, a = self._fork_target(prompt, label)
        return await self._client.chat_async(
            p, new_session=True, **self._opts(a, kw))

    def fork_stream(self, prompt: str, *, label: str = "", **kw):
        p, a = self._fork_target(prompt, label)
        return self._client.chat_stream(p, new_session=True, **self._opts(a, kw))

    async def fork_many(self, prompts, *, concurrency: int = 4,
                        labels=None, **kw):
        """여러 독립 변형을 **병렬** 실행 (동시 개수 상한). 결과는 입력 순서대로.

        출력도 큰 복수 결과를 동시에 뽑을 때 — 각 변형은 전사록을 자기 세션에
        재시드(서로 안 섞임)하고, ``concurrency`` 로 동시 서브프로세스 수를
        제한한다(각 ``claude -p`` 가 무겁기 때문). wall-time 은 (상한 내에서)
        가장 느린 변형으로 수렴한다.

        labels 가 없으면 인덱스를 라벨로 써 alias 가 결정적·고유해진다(공유
        카운터 race 회피). 큰 출력을 파일로 흘리려면 대신 ``fork_stream`` 을
        항목별로 쓰거나, lean 을 끄고 에이전트에게 직접 파일을 쓰게 한다.

        실패 의미론: 어떤 fork 가 예외를 던지면 진행 중인 형제 fork 들은
        **취소**되고, 원래 예외 타입이 그대로 재전파된다 (동시 다중 실패 시
        첫 예외가 전파되며, 나머지는 ``__cause__`` 의 ExceptionGroup 에서
        확인 가능).
        """
        prompts = list(prompts)
        if labels is None:
            labels = [str(i) for i in range(len(prompts))]
        elif len(labels) != len(prompts):
            # 길이 불일치면 zip 이 조용히 잘라 prompt 를 누락시킨다 → 명시적 거부.
            raise ValueError(
                f"labels length ({len(labels)}) must match prompts "
                f"({len(prompts)})")
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(prompt, label):
            async with sem:
                return await self.fork_async(prompt, label=label, **kw)

        # gather(..., return_exceptions=False) 는 한 fork 가 예외를 던지면 그
        # 예외만 전파하고 나머지 in-flight fork(실제 서브프로세스)는 취소하지
        # 않은 채 계속 돈다 → TaskGroup 으로 교체해 형제 취소를 보장한다.
        # TaskGroup 은 실패를 ExceptionGroup 으로 감싸므로, gather 시절과 같은
        # "원본 예외 타입을 그대로 캐치" 하는 호출자 계약을 지키기 위해 첫 번째
        # 예외만 풀어서 재전파한다.
        tasks: list[asyncio.Task] = []
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(_one(p, lbl))
                         for p, lbl in zip(prompts, labels)]
        except* BaseException as eg:
            raise eg.exceptions[0] from eg
        return [t.result() for t in tasks]
