# agentcli 요리책 — 상황별 처방전

[English](cookbook.md) | [한국어](cookbook.ko.md)

"내 상황"을 아래에서 찾아 그대로 따라 하면 되는 문서다. 왜 그런지(원리·실측
수치)는 [README의 성능 가이드](../README.ko.md#성능-가이드-claude)에 있다.
모든 처방은 v0.7.3+ 기준.

## 빠른 찾아보기

| 상황 | 처방 한 줄 |
|---|---|
| [1. 대량 텍스트 정리/변환 배치](#1-대량-텍스트-정리변환-배치) | `env="lean"` — 툴은 느려지게만 한다 |
| [2. 서비스에 임베드하는 툴 에이전트](#2-서비스에-임베드하는-툴-에이전트) | 기본값 그대로 + 필요한 것만 명시 |
| [3. 내 컴퓨터 환경을 그대로 쓰는 도구](#3-내-컴퓨터-환경을-그대로-쓰는-도구) | `env="inherit"` 명시 |
| [4. 같은 지시문으로 수백 번 호출](#4-같은-지시문으로-수백-번-호출) | `system_prompt` 분리 + 캐시 안정화 |
| [5. 연속 호출의 부팅 비용 없애기](#5-연속-호출의-부팅-비용-없애기) | warm 세션 |
| [6. warm에서 긴 응답 받기](#6-warm에서-긴-응답-받기) | `stream_limit` (기본 8MiB) |
| [7. "느려요" — 어디부터 볼까](#7-느려요--어디부터-볼까) | 진단 순서 3단계 |
| [8. Windows에서 유난히 느리거나 멈춤](#8-windows에서-유난히-느리거나-멈춤) | CLI 업그레이드 + 타임아웃 420초 |
| [9. 병렬로 여러 개 돌리기](#9-병렬로-여러-개-돌리기) | 동시 3–4개 + 캐시 워밍 |
| [10. 품질이 떨어졌을 때](#10-품질이-떨어졌을-때) | effort 내리지 말고 원인부터 |
| [11. 토큰 요금이 궁금할 때](#11-토큰-요금이-궁금할-때) | `resp.tokens` 를 본다 |
| [12. 구버전에서 업그레이드](#12-구버전에서-업그레이드) | 브레이킹 1개 확인 |

---

## 1. 대량 텍스트 정리/변환 배치

표 재작성, 요약, 분류, 추출처럼 **텍스트가 들어가서 텍스트가 나오는** 작업.
툴이 있으면 모델이 툴로 방황해 몇 배 느려진다(실측: 같은 2만자 작업이
inherit 6분 vs lean 2분 25초).

```python
from agentcli.providers.claude import ClaudeProvider
from agentcli.types import Message

p = ClaudeProvider(env="lean")                 # 커스터마이즈도 툴도 없음
r = p.invoke([Message(role="user", content=프롬프트)],
             model="sonnet", timeout=300)
```

- 남는 시간은 거의 전부 **출력 생성**이다 — 더 줄이려면 호출당 항목 수를
  줄이거나 품질이 허용하는 선에서 `model="haiku"`.
- 지시문이 매번 같다면 → [4번](#4-같은-지시문으로-수백-번-호출)과 조합.
- 호출이 수십 번 이상이라면 → [5번](#5-연속-호출의-부팅-비용-없애기)과 조합.

## 2. 서비스에 임베드하는 툴 에이전트

FastAPI 등 호스트 앱 안에서 에이전트가 Bash/파일/자기 MCP 서버를 써야 하는
경우. **기본값(explicit)이 이 상황용으로 설계돼 있다** — 개발 머신에 깔린
MCP/skills가 따라 들어가지 않고, 지정한 것만 들어간다.

```python
p = ClaudeProvider(                            # env="explicit" 이 기본
    permission_mode="default",                 # 임베딩이면 권한도 좁히자
    allowed_tools=["Bash", "Read"],            # 빌트인 툴 정의를 이만큼만 로드
    mcp_config={"myserver": {"type": "http",   # 내 서비스 전용 MCP만
                             "url": "https://my/mcp"}},
)
```

- `allowed_tools`에 `mcp__myserver__*` 이름을 섞으면 자동으로 권한 게이트
  방식으로 배선된다 — MCP 툴 좁히기가 그대로 동작.
- 머신마다 결과가 달라지는 문제(재현성)가 이 기본값으로 사라진다.

## 3. 내 컴퓨터 환경을 그대로 쓰는 도구

내 머신의 CLAUDE.md, skills, MCP 서버를 **일부러** 쓰고 싶은 개발 보조
도구라면 상속을 명시한다:

```python
p = ClaudeProvider(env="inherit")              # 0.7.2 이전의 기본 동작
```

- 대가: 호스트 구성에 따라 턴당 토큰이 수만~수십만까지 부풀 수 있고, 다른
  머신에서는 다르게 동작한다. 그게 원하는 것일 때만 쓴다.

## 4. 같은 지시문으로 수백 번 호출

수 KB짜리 스펙/지시문을 매 호출 보내는 배치라면 두 가지를 켠다:

```python
p = ClaudeProvider(
    exclude_dynamic_system_prompt=True,        # git 상태가 바뀌어도 캐시 유지
)
r = p.invoke(
    [Message(role="system", content=큰_지시문),   # ← 프롬프트에 섞지 말고
     Message(role="user", content=작은_데이터)],  #    system 으로 분리
    model="sonnet")
print(r.tokens.cached_tokens)                  # 캐시를 실제로 탔는지 확인
```

- **배치 도중 모델과 effort를 바꾸지 말 것** — 둘 다 캐시 키라서 바꾸는
  순간 전체 캐시 미스.
- 캐시 TTL은 구독 인증 기준 1시간이고 히트마다 연장된다.
- `exclude_dynamic_system_prompt`는 구버전 CLI(플래그 없음)에서 에러가
  나므로 옵트인이다 — Claude Code 2.1.229+에서 확인됨.

## 5. 연속 호출의 부팅 비용 없애기

`claude -p`는 호출마다 하네스를 부팅한다(~2초, 구버전 3~11초). 호출이 많은
파이프라인은 프로세스를 한 번만 띄우는 warm 세션으로:

```python
from agentcli.providers.warm import open_warm

s = await open_warm(append_system_prompt=지시문)   # lean 이 기본
for 항목 in 배치:
    text = await s.send(항목)                      # 부팅 없이 턴만
await s.close()
```

- 한 세션은 직렬이다 — 병렬이 필요하면 여러 개 연다.
- 이전 항목의 맥락을 끊고 싶으면 `await s.send("/clear")`.

## 6. warm에서 긴 응답 받기

v0.7.3부터 응답 이벤트 한 줄의 한도가 8MiB라(이전 64KiB — 긴 응답이
`LimitOverrunError`로 죽던 원인) 보통은 신경 쓸 일이 없다. 정말 더 큰
응답이 필요하면:

```python
s = await open_warm(stream_limit=32 * 1024 * 1024)
```

- 한도를 넘으면 `WarmSessionError`가 나며, 그 세션은 닫고 다시 열어야 한다.

## 7. "느려요" — 어디부터 볼까

순서대로 하나씩:

1. **버전** — agentcli v0.7.3+ 인가? CLI(`claude --version`)가 2.1.229+
   인가? (구버전은 부팅이 수 배 느리고, 2.1.220/221은 [8번](#8-windows에서-유난히-느리거나-멈춤)의 스톨 버그가 있다.)
2. **티어** — 데이터 작업인데 `env="lean"`이 아닌가? `r.tokens.prompt_tokens`
   가 작업 대비 비정상적으로 크면(수만~) 환경 상속/툴 정의가 실려 있는 것.
3. **출력량** — 위 둘이 정상인데도 느리면 `r.tokens.completion_tokens`를
   본다. 출력 토큰이 크면 그게 시간이다 — 호출당 항목 수를 줄이거나 더
   빠른 모델을 쓴다. 그래도 원인을 모르겠으면
   `ClaudeProvider(debug=True, debug_log_path=...)`로 타임라인을 뜬다.

## 8. Windows에서 유난히 느리거나 멈춤

- Claude Code **2.1.220/221에는 headless `-p`가 ~405초 스톨하는 미해결
  버그**(claude-code#83859)가 있다. 이 버전이면 CLI부터 올린다. 올릴 수
  없으면 타임아웃을 420초 이상으로 잡아 스톨이 "행"으로 오인되지 않게 한다.
- 32,767자 argv 한계, stdin 행 같은 고전 문제는 라이브러리가 알아서
  처리한다(8,000바이트 초과 프롬프트는 자동으로 stdin 경유) — 별도 조치
  불필요.

## 9. 병렬로 여러 개 돌리기

레이트리밋은 계정 풀 단위다. 커뮤니티 보고 기준 동시 spawn 3–4개를 넘는
버스트는 유료 최상위 티어에서도 429를 맞는다.

- 동시 3–4개로 제한하는 큐 + 시작 시차를 둔다.
- 같은 디렉토리(cwd)의 세션은 서버 캐시를 공유한다 — **첫 호출 하나를 먼저
  완주시켜 캐시를 만든 뒤** 나머지를 푸는 순서가 빠르고 싸다.

## 10. 품질이 떨어졌을 때

빨라졌는데 결과가 나빠졌다면, 순서대로 의심한다:

1. **티어를 낮추면서 필요한 컨텍스트가 끊겼나** — 작업이 CLAUDE.md/skills에
   의존했다면 `env="inherit"`로 돌려보고 비교.
2. **모델을 낮췄나** — 되돌린다. 실사례: sonnet이 보안 기사를 오탐 거부해서
   11% 느린 opus가 옳은 선택이었다.
3. **effort/thinking을 낮췄나** — thinking 하드 오프는 벤치마크에서
   **정확도 10–15%p 손실**이 확인돼 있다. 자기 작업 기준 A/B 없이 낮추지
   말고, 낮춘다면 `medium`까지만(공식 앵커: Sonnet 5 medium ≈ Sonnet 4.6
   high).

## 11. 토큰 요금이 궁금할 때

모든 응답에 실측 수치가 실려 온다 — 추측하지 말 것:

```python
r = p.invoke(...)
t = r.tokens
print(t.prompt_tokens,        # 실제 입력 컨텍스트 전체
      t.cached_tokens,        # 그중 캐시에서 읽은 양 (~10% 요금)
      t.cache_creation_tokens,  # 캐시에 새로 쓴 양
      t.completion_tokens)    # 출력 (시간의 주범)
```

- `cached_tokens`가 0에 가깝다면 [4번](#4-같은-지시문으로-수백-번-호출)을
  적용할 여지가 있다는 뜻이다.

## 12. 구버전에서 업그레이드

```bash
pip install "agentcli-py @ git+https://github.com/saintiron82/agentcli.git@v0.7.4"
```

확인할 브레이킹은 **하나**다(0.7.2): claude provider가 더 이상 호스트 환경을
기본 상속하지 않는다. 업그레이드 후 —

- 대부분의 임베딩 코드: 그대로 두면 된다(더 빨라지고 재현성이 생긴다).
- 호스트 MCP/skills/CLAUDE.md에 **의존하던** 코드: `env="inherit"` 한 줄 추가.
- `lean=True` / `isolated=True` 를 쓰던 코드: 그대로 동작한다(티어 별칭).
- v0.6.x에서 온다면: warm 세션(0.7.1), 캐시 토큰 가시성(0.7.2), 부팅 1초
  단축(0.7.3)이 덤으로 따라온다.
