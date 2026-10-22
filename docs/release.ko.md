# 릴리즈 체크리스트

[English](release.md) | [한국어](release.ko.md)

이 프로젝트는 두 단계로 배포하는 것이 적절합니다.

## 1단계: GitHub-first 공개 릴리즈

API가 아직 beta이고 실제 Claude Code, Codex, GitHub Copilot CLI 환경에서 외부 검증이 더 필요한 동안 이 방식을 먼저 사용합니다.

1. 릴리즈 가능한 작업 트리를 커밋합니다.
2. `https://github.com/saintiron82/agentcli` 저장소를 만들거나 공개 전환합니다.
3. 커밋에 태그를 붙입니다.

   ```bash
   git tag v0.7.6
   git push origin main
   git push origin v0.7.6
   ```

4. Release 워크플로의 테스트·빌드 게이트가 통과하고 wheel·소스 패키지를
   첨부한 GitHub Release `v0.7.6`가 생성되는지 확인합니다. 릴리스 노트는
   [docs/releases/v0.7.6.ko.md](releases/v0.7.6.ko.md)를 사용합니다.

5. 사용자는 다음 명령으로 설치할 수 있습니다.

   ```bash
   pip install "agentcli-py @ git+https://github.com/saintiron82/agentcli.git@v0.7.6"
   ```

## 2단계: PyPI 릴리즈

GitHub 릴리즈로 기본 외부 검증을 받은 뒤, 패키지 이름을 안정적인 public install path로 고정할 때 사용합니다.

1. PyPI에서 `agentcli-py` pending Trusted Publisher를 설정합니다 (아래 참조).
   pending publisher는 이름을 예약하지 않으며 최초 업로드 때 프로젝트가
   생성됩니다. 수동 업로드에는 API 토큰을 사용할 수도 있습니다.
2. 배포 파일을 빌드하고 검사합니다.

   ```bash
   python -m pip install -e ".[dev]"
   python -m pytest
   python -m build
   python -m twine check dist/*
   ```

3. 업로드합니다.

   ```bash
   python -m twine upload dist/*
   ```

4. 사용자는 다음 명령으로 설치할 수 있습니다.

   ```bash
   pip install agentcli-py
   ```

## 자동 릴리즈 (GitHub Actions)

`.github/workflows/release.yml`이 `v*` 태그 푸시에서 GitHub Release를 생성합니다.
PyPI는 별도로 활성화하는 게시 경로입니다.

```bash
git tag v0.7.6
git push origin main
git push origin v0.7.6
```

워크플로 동작:

1. **build + verify** — `".[dev]"` 설치 후 `pytest`를 게이트로 실행(실패한
   태그는 배포되지 않음), `python -m build`, `twine check dist/*`.
2. **github-release** — 빌드 검증 성공 후 `docs/releases/<tag>.ko.md`를 우선
   사용하고, 없으면 `docs/releases/<tag>.md`를 사용해 빌드 산출물을 첨부한
   GitHub Release를 생성합니다. 릴리스 본문에서는 저장소 상대 경로가 올바르게
   연결되지 않으므로 언어 전환 링크에 GitHub 절대 URL을 사용합니다.
   PyPI 비활성화나 게시 실패는 이 잡을 막지 않습니다.
3. **pypi-publish** — repository variable `PYPI_PUBLISH_ENABLED`가 `true`일
   때만 `pypa/gh-action-pypi-publish`로 **PyPI Trusted
   Publishing (OIDC)** 업로드. `twine upload`가 아니며, 저장되는 API 토큰도
   없습니다. `twine`은 1단계의 `check` 용도로만 사용됩니다.

PyPI 활성화 전 1회 설정 (PyPI → *Publishing* → pending publisher 추가): project
`agentcli-py`, owner `saintiron82`, repo `agentcli`, workflow `release.yml`,
environment `pypi`. 새 릴리스 태그를 푸시하기 전에 GitHub repository variable
`PYPI_PUBLISH_ENABLED=true`를 설정합니다. GitHub 전용 릴리스에서는 이 값을
설정하지 않거나 `false`로 유지합니다. GitHub Release가 있다고 PyPI에도 게시된
것으로 간주하지 않습니다. API 토큰 방식을 쓰려면 `pypi-publish` 잡에서
`permissions.id-token` + `environment`를 제거하고 publish 스텝에
`password: ${{ secrets.PYPI_API_TOKEN }}`를 전달합니다.

## 현재 추천

먼저 1단계를 권장합니다. PyPI 등록을 기다리지 않고 태그 소스나 첨부 wheel로
설치할 수 있습니다. 버전 태그를 만들기 전에 `pyproject.toml`, 한·영 README
설치 예시, 한·영 릴리스 노트를 갱신하고 커밋합니다. 이미 게시한 버전의 태그는
다른 내용으로 이동하지 않습니다.
