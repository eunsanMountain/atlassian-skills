# atlassian-skills — CLAUDE.md

## 프로젝트 개요

사내 Atlassian Server/DC 스택(Jira, Confluence, Bitbucket, Bamboo)을 LLM 에이전트가 토큰 효율적으로 다루기 위한 Python CLI + Claude Code Skill. `mcp-atlassian` MCP 서버를 완전 대체한다.

- **바이너리**: `atls`
- **패키지**: `atlassian-skills`
- **현재 버전**: 0.1.2 (Jira/Confluence read+write, MCP 완전 대체)

## 빌드 & 실행

```bash
# 설치 (uv 권장)
uv sync

# CLI 실행
uv run atls --help
uv run atls jira issue get PROJ-1 --format=compact

# 테스트
uv run pytest                          # unit + contract + snapshot
uv run pytest -m integration           # 사내 인스턴스 실호출 (수동)
uv run pytest tests/benchmarks         # 토큰 벤치마크

# 린트 & 타입체크
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
```

## 프로젝트 구조

```
src/atlassian_skills/
├── cli/                    # Typer CLI (main.py = 엔트리, 제품별 분리)
├── core/
│   ├── client.py           # BaseClient (httpx, retry, auth, pagination)
│   ├── auth.py             # PAT/Basic, env > keyring > config
│   ├── config.py           # ~/.config/atlassian-skills/config.toml
│   ├── errors.py           # AtlasError, exit code §15.4
│   ├── pagination.py       # offset + link-follow 패턴
│   └── format/             # compact, md, json, raw 포맷터
├── jira/
│   ├── client.py           # JiraClient(BaseClient)
│   ├── models.py           # pydantic 응답 모델
│   └── preprocessing.py    # 멘션·스마트링크 전처리 (§5.1.1)
├── confluence/
│   ├── client.py
│   └── models.py
├── bitbucket/              # 0.2.0
└── bamboo/                 # 0.2.0

tests/
├── fixtures/               # MCP 캡처 JSON (골든 파일)
├── unit/                   # respx 모킹
├── contract/               # 응답 ↔ pydantic 모델
├── snapshot/               # syrupy CLI 출력 회귀
└── benchmarks/             # tiktoken 토큰 측정

skills/atls/SKILL.md        # Claude Code 디스패처 skill
docs/                       # 설계 문서, 분석서
```

## 코딩 컨벤션

### Python 스타일
- **Python 3.10+**, type hints 필수 (`disallow_untyped_defs = true`)
- **ruff** 포매터 + 린터 (line-length=120)
- **pydantic v2** 모델 (응답 파싱, `model_validate`)
- `from __future__ import annotations` 모든 모듈 상단에
- import 순서: stdlib → third-party → local (ruff isort가 강제)

### 네이밍
- 모듈/변수: `snake_case`
- 클래스: `PascalCase`
- CLI 명령: `kebab-case` (Typer가 자동 변환)
- 상수: `UPPER_SNAKE_CASE`

### 에러 처리
- 모든 API 에러는 `AtlasError` 계열로 래핑
- exit code는 §15.4 규약 준수 (0=OK, 2=not found, 6=auth, ...)
- `--format=json` 시 에러도 JSON envelope로 stdout 출력

### 테스트
- unit 테스트: `respx`로 httpx 모킹, fixtures/ JSON 사용
- snapshot 테스트: `syrupy`로 CLI 출력 회귀 방지
- 토큰 벤치마크: `tiktoken` cl100k_base, 상한 초과 시 fail
- `@pytest.mark.integration`은 CI에서 skip, 수동 실행

## 핵심 설계 원칙

1. **CLI가 본체** — skill은 CLI를 호출하는 얇은 래퍼
2. **토큰 효율** — compact 포맷 기본, MCP 대비 L1≥50% 절감
3. **cfxmark 단일 의존성** — Confluence XHTML + Jira wiki 변환 모두 cfxmark (>=0.4)
4. **Server/DC 전용** — Cloud 호환은 비목표
5. **byte-preserving raw** — `--format=raw`는 서버 응답을 1byte도 바꾸지 않음

## 주요 의존성

| 패키지 | 용도 |
|---|---|
| `httpx` | REST 클라이언트 (sync) |
| `typer` + `rich` | CLI 프레임워크 |
| `pydantic` | 응답 모델 |
| `cfxmark>=0.4` | Confluence XHTML ↔ md, Jira wiki ↔ md |
| `platformdirs` | config 경로 |

## 인증 (§7.1)

```bash
# 환경변수 (기본, 권장)
export ATLS_CORP_JIRA_TOKEN="your-pat"
export ATLS_CORP_CONFLUENCE_TOKEN="your-pat"

# config.toml
# ~/.config/atlassian-skills/config.toml
# [profiles.corp]
# jira_url = "https://jira.corp.example.com"
```

우선순위: CLI 플래그 > 환경변수 > keyring > config 평문

## 릴리즈 프로세스

CI와 PyPI 배포가 GitHub Actions로 자동화됨 — 수동 `uv build` / `twine upload` 불필요.

```bash
# 1. CHANGELOG.md 상단에 ## [X.Y.Z] - YYYY-MM-DD 섹션 추가
# 2. pyproject.toml version = "X.Y.Z"
# 3. uv sync --all-extras  (uv.lock 갱신)
# 4. 커밋 & push (CI가 ruff + mypy + pytest 3.10-3.13 검증)
git commit -am "chore: release vX.Y.Z"
git push origin main

# 5. 태그 push → release.yml 자동 트리거
git tag vX.Y.Z && git push origin vX.Y.Z
```

**release.yml이 자동 수행**:
1. 태그 버전 ↔ `pyproject.toml` version 일치 검증
2. 테스트 재실행
3. `uv build` → PyPI publish (`PYPI_TOKEN` secret)
4. CHANGELOG에서 해당 버전 섹션 추출해 GitHub Release 본문으로 사용
5. wheel + sdist를 Release 에셋으로 첨부

**워크플로우 파일**: `.github/workflows/{ci,release}.yml`

## 참고 문서

- `docs/DESIGN.md` — 전체 설계 (§1-§16, 부록 A-E)
- `docs/mcp-analysis.md` — MCP 토큰 낭비 분석
- `docs/api-endpoint-mapping.md` — REST 엔드포인트 매핑 (68건)
- `references/` — mcp-atlassian, cfxmark, atlassian-python-api 등 참고 레포 (gitignored)
