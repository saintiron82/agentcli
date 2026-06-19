# 릴리즈 체크리스트

[English](release.md) | [한국어](release.ko.md)

이 프로젝트는 두 단계로 배포하는 것이 적절합니다.

## 1단계: GitHub-first 공개 릴리즈

API가 아직 beta이고 실제 Claude Code, Codex, GitHub Copilot CLI 환경에서 외부 검증이 더 필요한 동안 이 방식을 먼저 사용합니다.

1. 릴리즈 가능한 작업 트리를 커밋합니다.
2. `https://github.com/saintiron82/agentcli` 저장소를 만들거나 공개 전환합니다.
3. 커밋에 태그를 붙입니다.

   ```bash
   git tag v0.4.2
   git push origin main --tags
   ```

4. [docs/releases/v0.4.2.ko.md](releases/v0.4.2.ko.md)를 기준으로 GitHub Release `v0.4.2`을 간단히 만듭니다.

5. 사용자는 다음 명령으로 설치할 수 있습니다.

   ```bash
   pip install "agentcli @ git+https://github.com/saintiron82/agentcli.git@v0.4.2"
   ```

## 2단계: PyPI 릴리즈

GitHub 릴리즈로 기본 외부 검증을 받은 뒤, 패키지 이름을 안정적인 public install path로 고정할 때 사용합니다.

1. PyPI에서 `agentcli` 프로젝트를 예약하거나 생성합니다.
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
   pip install agentcli
   ```

## 자동 릴리즈 (GitHub Actions)

`.github/workflows/release.yml`이 `v*` 태그 푸시에서 두 단계를 모두 수행하므로,
위의 수동 명령은 fallback 경로입니다.

```bash
git tag v0.5.1
git push origin main --tags
```

워크플로 동작:

1. **build + verify** — `".[dev]"` 설치 후 `pytest`를 게이트로 실행(실패한
   태그는 배포되지 않음), `python -m build`, `twine check dist/*`.
2. **pypi-publish** — `pypa/gh-action-pypi-publish`로 **PyPI Trusted
   Publishing (OIDC)** 업로드. `twine upload`가 아니며, 저장되는 API 토큰도
   없습니다. `twine`은 1단계의 `check` 용도로만 사용됩니다.
3. **github-release** — PyPI 업로드 성공에 게이트되며,
   `docs/releases/<tag>.md`를 기준으로 빌드 산출물을 첨부해 GitHub Release를
   생성합니다.

첫 태그 전 1회 설정 (PyPI → *Publishing* → pending publisher 추가): project
`agentcli-py`, owner `saintiron82`, repo `agentcli`, workflow `release.yml`,
environment `pypi`. API 토큰 방식을 쓰려면 `pypi-publish` 잡에서
`permissions.id-token` + `environment`를 제거하고 publish 스텝에
`password: ${{ secrets.PYPI_API_TOKEN }}`를 전달합니다.

## 현재 추천

먼저 1단계를 권장합니다. `pip` 설치는 가능하게 유지하면서, PyPI 이름을 고정하기 전에 실제 사용 환경에서 작은 검증을 받을 수 있습니다.
