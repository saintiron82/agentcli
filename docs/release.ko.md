# 릴리즈 체크리스트

[English](release.md) | [한국어](release.ko.md)

이 프로젝트는 두 단계로 배포하는 것이 적절합니다.

## 1단계: GitHub-first 공개 릴리즈

API가 아직 beta이고 실제 Claude Code, Codex, GitHub Copilot CLI 환경에서 외부 검증이 더 필요한 동안 이 방식을 먼저 사용합니다.

1. 릴리즈 가능한 작업 트리를 커밋합니다.
2. `https://github.com/saintiron82/agentcli` 저장소를 만들거나 공개 전환합니다.
3. 커밋에 태그를 붙입니다.

   ```bash
   git tag v0.4.1
   git push origin main --tags
   ```

4. [docs/releases/v0.4.1.ko.md](releases/v0.4.1.ko.md)를 기준으로 GitHub Release `v0.4.1`을 간단히 만듭니다.

5. 사용자는 다음 명령으로 설치할 수 있습니다.

   ```bash
   pip install "agentcli @ git+https://github.com/saintiron82/agentcli.git@v0.4.1"
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

## 현재 추천

먼저 1단계를 권장합니다. `pip` 설치는 가능하게 유지하면서, PyPI 이름을 고정하기 전에 실제 사용 환경에서 작은 검증을 받을 수 있습니다.
