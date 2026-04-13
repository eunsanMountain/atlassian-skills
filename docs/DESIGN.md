# atlassian-skills — DESIGN

> 사내 Atlassian 스택(Jira / Confluence / Bitbucket Server / Bamboo)을 LLM 에이전트가 토큰 효율적으로 다루기 위한 Python CLI + Claude Code Skill.

상태: **Draft v0.1** — 2026-04-08
저자: eunsan.jo

---

## 1. 목표 (Goals)

1. **MCP 대체** — 현재 `mcp-atlassian` MCP 서버가 노출하는 Jira/Confluence 동작과 **기능적으로 동등**하게 작동.
2. **토큰 효율** — MCP 대비 응답/스키마 토큰을 대폭 절감. 측정 가능한 메트릭으로 검증.
3. **범위 확장** — MCP가 빠뜨린 **Bitbucket Server**와 **Bamboo**를 1급 시민으로 포함.
4. **Server/DC 전용** — 사내 환경 타겟. Cloud 호환은 비목표(필요시 추후).
5. **오픈소스 배포** — PyPI 공개. MIT.

### 비목표 (Non-Goals)
- Bitbucket Cloud API 2.0 지원
- Atlassian Cloud OAuth 흐름
- GUI / 웹 UI
- MCP 서버 구현 자체 (CLI + Skill로 대체)

---

## 2. 핵심 결정 사항 (Key Decisions)

| # | 결정 | 비고 |
|---|---|---|
| D1 | **CLI가 본체**, Skill은 CLI를 호출하는 얇은 래퍼 | MCP 토큰 비효율 회피 |
| D2 | **Python**, `uv` + `pyproject.toml` | |
| D3 | 패키지명 `atlassian-skills`, 바이너리 `atls` | PyPI 패키지명/스크립트명 분리 |
| D4 | 단일 바이너리 + 서브커맨드 트리 `atls <product> <resource> <verb>` | 네 제품 통합 |
| D5 | **자체 `httpx` REST 클라이언트로 4개 제품 모두 통일** | 출력 가공 일관성, Bitbucket Server 갭 회피 |
| D6 | `atlassian-python-api`는 **참고용 매핑 사전**으로만 사용, 런타임 의존성 X | |
| D7 | 출력 포맷: `compact`(기본) / `md`(본문) / `json`(스크립팅) | 토큰 효율 |
| D8 | 본문 변환은 **cfxmark 단일 의존성**으로 통합: Confluence=cfxmark(기존), Jira wiki ↔ md=cfxmark(보강된 Jira wiki 렌더러). Jira 서버 특화 전처리(멘션·스마트링크·HTML 정리)는 `jira/preprocessing.py` 자체 레이어. | §5.1 |
| D9 | 인증: 제품별 매트릭스 §7.1로 고정. PAT(Bearer) 기본, Basic(user+token) opt-in | Server/DC 전용 |
| D10 | Skill 구조: **얇은 디스패처 1개**로 시작, 워크플로우만 별도 skill 승격 | |
| D11 | **0.1.0 컷라인 = Jira/Confluence read+write + RFE-001 고수준 명령 (MCP 완전 대체)**. Bitbucket/Bamboo는 0.2.0 | §10 게이트 참고 |
| D12 | 테스트: 자체 작성. Jira/Confluence는 **MCP 호출 결과를 픽스처/골든**으로 사용 | |

---

## 3. 사용자 & 사용 모드

- **주 사용자**: LLM 에이전트(Claude Code)가 셸을 통해 호출. 사람도 보조적으로 사용.
- **호출 경로**:
  1. Claude → Bash → `atls jira issue get PROJ-1 --format=compact`
  2. Claude → `/atls` skill → 디스패처가 적절한 명령으로 안내
  3. 사람 → 터미널 직접 실행

---

## 4. 명령 트리 (Command Tree)

```
atls
├── auth
│   ├── login            # 프로필별 토큰 저장
│   ├── status           # 현재 프로필/연결 상태
│   └── list             # 등록된 프로필 목록
├── config
│   ├── get / set / path
├── jira
│   ├── issue            # get | search | create | update | delete | transitions | transition | dates | sla | images
│   ├── issue-batch      # create | get-changelogs            (batch_create_issues, batch_get_changelogs)
│   ├── dev-info         # get | get-many                     (get_issue_development_info[, _issues_])
│   ├── comment          # list | add | edit | delete
│   ├── worklog          # list | add
│   ├── attachment       # list | download | upload | delete
│   ├── link             # list-types | create | delete | remote-create   (create_remote_issue_link)
│   ├── epic             # link                                (link_issue_to_epic)
│   ├── watcher          # list | add | remove
│   ├── project          # list | get | versions | versions-batch | components
│   ├── board            # list | issues
│   ├── sprint           # list | issues | create | update | add-issues
│   ├── field            # search | options
│   ├── user             # get
│   ├── service-desk     # list | queues | queue-issues
│   └── form             # list | get | answer                 (proforma_*)
├── confluence
│   ├── page             # get | search | children | create | update | delete | move | history | diff | views | images
│   │                    # push-md | pull-md | diff-local   (RFE-001 고수준 명령)
│   ├── space            # tree
│   ├── comment          # list | add | reply
│   ├── label            # list | add
│   ├── attachment       # list | download | download-all | upload | upload-batch | delete
│   └── user             # search
├── bb                   # Bitbucket Server (1.0)
│   ├── project          # list | get
│   ├── repo             # list | get | branches | tags | browse
│   ├── pr               # list | get | create | update | diff | commits | activities | approve | unapprove | merge | decline
│   ├── pr-comment       # list | add | reply | delete
│   ├── commit           # get | diff | comments
│   └── user             # get
└── bamboo
    ├── project          # list | get
    ├── plan             # list | get | branches | enable | disable
    ├── build            # latest | result | trigger | stop | queue
    ├── deploy           # projects | environments | trigger
    ├── label / comment  # list | add
    └── agent            # status
```

전역 옵션: `--profile`, `--format`, `--limit`, `--fields`, `--no-color`, `--quiet`, `-v/-vv`.

---

## 5. 출력 포맷 (Output Strategy)

| 모드 | 트리거 | 용도 | 특징 |
|---|---|---|---|
| `compact` | 기본 | LLM 스캔/필터링 | 키:값 한 줄, ANSI 없음, 핵심 6~8개 필드만 |
| `md` | `--format=md` 또는 본문 조회 | 본문(description, page body, comment) | §5.1 변환 정책 적용, 인라인 이미지는 placeholder |
| `json` | `--format=json` | 스크립팅, 정확 필드 | pydantic 모델 직렬화, indent 없음 |
| `raw` | `--format=raw` | 디버깅/fallback, **byte-preserving body 조회** | 변환 없이 원본 문자열 (Jira wiki, ADF, Confluence storage). **0.1.0 보장사항** — §15.3 참조 |

### 5.1 본문 변환 정책 (Body Conversion)

본문 변환은 cfxmark 단일 의존성으로 통합한다. cfxmark의 Jira wiki 렌더러 보강(R1-R7, `docs/cfxmark-jira-enhancement-requirements.md` 참고)을 전제로 한다. Jira 서버 특화 전처리는 cfxmark 호출 전·후에 atlassian-skills 자체 레이어에서 수행한다 (§5.1.1).

| 소스 | 변환 경로 | 전처리 | fallback |
|---|---|---|---|
| Confluence storage XHTML (page body, comment) | **cfxmark `to_md()`** (default) | — | 실패 시 raw + warning |
| Confluence ADF (Cloud — 비목표지만 응답에 섞일 수 있음) | cfxmark ADF 경로 | — | raw |
| Jira wiki markup (description, comment.body — Server 기본) | **cfxmark `from_jira_wiki()`** | 멘션·스마트링크 전처리 (§5.1.1) | raw + warning |
| Jira ADF (rare on Server) | 미지원 → raw + warning | — | — |
| Jira plain text | passthrough | — | — |

**Write 경로 변환 규칙**:
- **Jira write (default)**: 입력은 md로 받고, **cfxmark `to_jira_wiki()`로 md→wiki 변환 후 PUT**한다. Server/DC Jira는 wiki markup 렌더링이 1급이므로 "서버가 확실히 이해하는 형식"을 우선한다. 코드블록 언어 정규화는 `code_language_map` 옵션으로 처리.
- `--body-format=wiki`: 입력을 변환 없이 그대로 PUT (이미 wiki로 작성한 경우).
- `--body-format=raw`: 변환/검증 없이 passthrough (디버깅/fallback).
- `--body-format=md`: 명시적으로 변환 경로 요청 (default와 동일).
- **Confluence write**: storage XHTML이 source-of-truth이므로 md 입력은 cfxmark 역변환 → storage로 PUT. `--body-format=storage`로 passthrough 가능.
- Round-trip 손실 가능성을 `--format=md` 출력 footer로 표시: `[converted: jira-wiki→md, lossy]`.

**cfxmark 옵션 CLI 노출** (RFE-001):
- `--passthrough-prefix=<prefix>`: cfxmark `ConversionOptions(passthrough_html_comment_prefixes=(...))` 전달. `<!-- workflow:meta -->` 등의 HTML 코멘트를 변환 과정에서 보존. 복수 지정 가능. `cfxmark:` prefix는 거부 (cfxmark 자체 sentinel 보호). `--body-repr=raw`일 때는 무시.
- `--section=<heading>`: cfxmark `to_jira_wiki(section=...)` 전달. 지정 H2 섹션의 body만 추출. 섹션 없으면 exit 7 (validation). `--body-format=md`일 때만 유효.
- `--heading-promotion=confluence|jira|none`: cfxmark `to_jira_wiki(heading_promotion=...)` 전달. `--body-format=md`일 때만 유효.
- `--drop-leading-notice=<prefix>`: cfxmark `drop_leading_notice` 전달. 콤마 구분 복수 가능. `--body-format=md`일 때만 유효.

### 5.1.1 Jira 전처리 레이어 (Jira Preprocessing Layer)

cfxmark은 범용 마크업 변환 라이브러리이므로, Jira 서버 인스턴스에 특화된 전처리는 atlassian-skills 자체 레이어에서 수행한다. 구현 위치: `src/atlassian_skills/jira/preprocessing.py`.

**Read 경로** (서버 응답 → cfxmark 호출 전):

| 단계 | 처리 | 참고 |
|---|---|---|
| 1. 멘션 치환 | `[~accountid:X]` → `@user-X` (또는 display name lookup) | mcp-atlassian `_process_mentions` 참고 |
| 2. 스마트링크 정규화 | `[text\|url\|smart-link]` → `[text\|url]` (표준 Jira 링크 형태로 정규화) | mcp-atlassian `_process_smart_links` 참고 |
| 3. cfxmark 호출 | `from_jira_wiki(preprocessed_text)` → Markdown | — |
| 4. HTML 잔여물 정리 | cfxmark 출력에 남은 HTML fragment 처리 (선택적) | markdownify 또는 자체 구현 |

**Write 경로** (사용자 md 입력 → 서버 PUT 전):

| 단계 | 처리 | 참고 |
|---|---|---|
| 1. cfxmark 호출 | `to_jira_wiki(md_input, code_language_map=JIRA_LANG_MAP)` → wiki markup | — |
| 2. 멘션 역치환 | `@user-X` → `[~X]` (선택적) | — |

**의존성**: 이 레이어는 `httpx` 등 네트워크 의존성을 가질 수 있음 (display name 조회). cfxmark 자체는 순수 텍스트 변환으로 유지.

### 필드 프루닝 규칙
- **default 필드**: `key, status, assignee, summary, updated` (Jira) / `id, title, space, version, updated` (Confluence) / 등
- `--fields=all`: 전체 raw
- `--fields=key,summary,status,reporter`: 명시 필드만
- `--fields=+labels,+priority`: default에 추가
- **body 미요청 보장** (RFE-001 R7): `--fields`에 body/description이 포함되지 않으면 REST API의 `expand` 파라미터에서 body를 **생략**한다. 예: `--fields=version`일 때 Confluence는 `expand=version`만, Jira는 `fields=updated`만 요청. body fetch를 건너뛰면 응답 크기와 latency가 ~1/10 수준으로 감소하며, push 전 pre-flight version check 등에 사용.

### 5.2 토큰 벤치마크 규약 (Token Accounting Protocol)

회귀 방지·MCP 동등성 주장 둘 다 측정 대상이 명확해야 한다.

**측정 도구**: `tiktoken`, encoding `cl100k_base` (Claude/GPT-4 계열 근사).

**측정 대상 (3 layers, 분리 보고)**:

| Layer | 무엇을 카운트 | 비교 대상 |
|---|---|---|
| **L1 payload** | CLI/MCP가 LLM에 반환하는 **최종 출력 문자열** (compact / md / json) — 따옴표·중괄호·개행 포함 | atls 출력 vs MCP 툴 응답 문자열 |
| **L2 invocation** | 한 번의 호출에 동반되는 **tool/command schema 토큰** — atls는 `--help` 텍스트 또는 0(셸 명령), MCP는 tool description+inputSchema | atls dispatcher skill vs mcp-atlassian 전체 tool 리스트 |
| **L3 workflow** | 샘플 워크플로우(예: "이슈 1건 조회 + 댓글 3건 + 상태 전이") 한 세션의 L1+L2 합 | end-to-end 시나리오 5개 |

**고정 시나리오** (`tests/benchmarks/scenarios.py`에 박아둠):
1. `jira issue get PROJ-1` (single issue, default fields)
2. `jira issue search "project=PROJ AND status=Open" --limit=20`
3. `confluence page get <id>` (md 본문 포함)
4. `confluence search "term" --limit=10`
5. (워크플로우) issue get → comments list → transitions → transition

**목표**:
- L1: MCP 응답 대비 **≥ 50% 절감** (default `compact`)
- L2: atls dispatcher skill ≤ **2,000 토큰** (mcp-atlassian 전체 tool schema는 ~15k+)
- L3: 시나리오 합산 **≥ 60% 절감**

**보조 지표 (공정성 보강)**: 토큰 외에 같이 보고.
- `bytes`: UTF-8 byte count (인코딩 독립)
- `line count`: 개행 수 (compact 모드의 구조적 밀도 지표)
- `field preservation score`: MCP 응답의 핵심 필드 집합 대비 atls 출력의 커버리지 비율 (0.0~1.0). 토큰 절감이 정보 손실로 이루어지는 걸 막는 가드레일. 목표 ≥ 0.95.

**보고**: `pytest tests/benchmarks --benchmark-report` 가 markdown 표 생성, CI artifact로 저장. 회귀 시 fail.

---

## 6. 아키텍처

```
src/atlassian_skills/
├── __init__.py
├── cli/
│   ├── main.py             # Typer 엔트리포인트
│   ├── jira.py
│   ├── confluence.py
│   ├── bitbucket.py
│   └── bamboo.py
├── core/
│   ├── client.py           # BaseClient (httpx, retry, auth, pagination)
│   ├── auth.py             # 토큰 로드/저장, keyring 옵션
│   ├── config.py           # ~/.config/atlassian-skills/config.toml + profile
│   ├── errors.py
│   ├── pagination.py
│   ├── format/
│   │   ├── compact.py
│   │   ├── markdown.py     # cfxmark 통합 (Confluence + Jira 모두)
│   │   ├── json.py
│   │   └── raw.py          # passthrough (Jira wiki / Confluence storage)
│   └── tokens.py           # 토큰 카운트 측정 유틸
├── jira/
│   ├── client.py           # JiraClient(BaseClient)
│   ├── models.py           # pydantic 응답 모델
│   ├── preprocessing.py    # 멘션·스마트링크·HTML 전처리 (§5.1.1)
│   ├── issue.py / comment.py / sprint.py / ...
├── confluence/
│   ├── client.py
│   ├── models.py
│   └── page.py / ...
├── bitbucket/              # Server 1.0
│   ├── client.py
│   ├── models.py
│   └── pr.py / repo.py / ...
└── bamboo/
    ├── client.py
    ├── models.py
    └── plan.py / build.py / ...

skills/
└── atls/
    └── SKILL.md            # 디스패처 skill (~50줄)

tests/
├── fixtures/               # MCP 호출/공식 문서 응답 JSON
├── unit/
├── contract/               # 응답 ↔ pydantic 모델
├── snapshot/               # CLI 출력 회귀
└── integration/            # @pytest.mark.integration
```

### BaseClient 책임
- httpx 세션, base_url, 인증 헤더 (§7.1 매트릭스)
- 재시도(429/5xx, 지수 백오프)
- 페이지네이션 (Atlassian의 `start/limit` + `isLastPage` 패턴)
- 표준 에러 → `AtlasError` 매핑
- 응답 → pydantic 모델 → 포맷터 파이프라인

---

## 7. 인증 & 프로필

### 7.1 인증 매트릭스 (Server/DC 전용)

`auth_method` 값은 프로필 단위 또는 제품별로 override 가능. **default는 Bamboo를 제외한 모든 제품이 `pat`**, Bamboo만 `basic` (PAT 미지원 버전 호환).

| 제품 | 지원 auth_method | username 필요 | Authorization 헤더 | write 지원 | 비고 |
|---|---|---|---|---|---|
| Jira | `pat` (default), `basic` | `pat`=N, `basic`=Y | `pat`: `Bearer <token>` / `basic`: `Basic base64(user:token)` | Y | Server PAT는 8.14+ |
| Confluence | `pat` (default), `basic` | `pat`=N, `basic`=Y | 동일 | Y | Server PAT는 7.9+ |
| Bitbucket Server | `pat` (default), `basic` | `pat`=N, `basic`=Y | 동일 | Y | PAT는 5.5+, project/repo permission 필요 |
| Bamboo | `basic` (default), `pat` | `basic`=Y, `pat`=N | 동일 | Y | Bamboo는 PAT 미지원 버전이 흔함 → basic이 안전 default |

**비지원 (명시적 비목표)**: Cloud OAuth 2.0 3LO/2LO, API key with email, Forge.

**credential shape**:
```python
class Credential:
    method: Literal["pat", "basic"]
    token: str               # PAT 또는 password
    username: str | None     # basic일 때 필수
```

**해석 우선순위**: CLI 플래그 > 환경변수 > keyring(opt-in) > config 평문(opt-in).

**환경변수 네이밍** (D14 확정):
```
ATLS_<PROFILE>_<PRODUCT>_TOKEN     # PAT 또는 password
ATLS_<PROFILE>_<PRODUCT>_USER      # basic일 때만
ATLS_<PROFILE>_<PRODUCT>_AUTH      # "pat" | "basic" override (선택)
```
예: `ATLS_CORP_JIRA_TOKEN`, `ATLS_CORP_BAMBOO_USER`, `ATLS_CORP_BAMBOO_TOKEN`.

### 7.1.1 `auth login` 동작 계약

CLI는 런타임에 부모 셸의 환경변수를 영속적으로 "저장"할 수 없다. 따라서 `auth login`은 저장소 백엔드를 **명시적 플래그로 선택**하는 계약으로 고정한다.

| 모드 | 트리거 | 동작 |
|---|---|---|
| **export snippet (default)** | `atls auth login --profile corp --product jira` | 프롬프트로 토큰 입력 → stdout에 `export ATLS_CORP_JIRA_TOKEN=...` 출력. 사용자는 `eval "$(atls auth login ...)"` 또는 shell rc에 붙여넣기. **디스크에 아무것도 쓰지 않음**. |
| **keyring** | `--keyring` (optional extra 설치 필요) | OS keyring에 저장. config는 `storage = "keyring"` 기록. |
| **config plaintext** | `--write-config` | `~/.config/atlassian-skills/config.toml`에 평문 저장 + stderr 경고. `storage = "plaintext"`. 파일 퍼미션 **0600 강제**; 기존 파일이 0600보다 넓으면 stderr 경고 후 0600으로 재설정. |

- `auth status`: 현재 프로필의 해석 결과 (어느 소스에서 credential이 오는지, 연결 ping 성공 여부) 리포트.
- `auth list`: 등록된 프로필 목록 + 각 프로필의 storage 모드.
- 비대화형 환경(`stdin`이 TTY가 아님)에선 프롬프트 대신 `ATLS_LOGIN_TOKEN` 환경변수를 읽거나 `--token-stdin`으로 파이프 받는다.
- **URL 스킴 검증**: `*_url` 설정이 `http://`인 경우, 자격증명이 평문 전송될 수 있으므로 매 요청마다 stderr 경고를 출력한다. 경고를 억제하려면 `--allow-insecure` 플래그가 필요하다. `https://`가 기본 기대값이며, `http://`를 거부하지는 않는다 (사내 reverse proxy 환경 고려).

### 7.2 config.toml 예시

```toml
# ~/.config/atlassian-skills/config.toml
default_profile = "corp"

[profiles.corp]
jira_url        = "https://jira.corp.example.com"
confluence_url  = "https://wiki.corp.example.com"
bitbucket_url   = "https://bitbucket.corp.example.com"
bamboo_url      = "https://bamboo.corp.example.com"

# 제품별 auth_method override (생략 시 §7.1 default)
[profiles.corp.auth]
jira       = "pat"
confluence = "pat"
bitbucket  = "pat"
bamboo     = "basic"

# credential 자체는 env/keyring 권장. 평문은 opt-in.
# storage = "env" | "keyring" | "plaintext"
storage = "env"
```

---

## 8. 의존성 스택

```toml
[project]
dependencies = [
  "httpx>=0.27",
  "typer>=0.12",
  "rich>=13",
  "pydantic>=2.5",
  "tomli; python_version < '3.11'",
  "tomli-w",
  "cfxmark>=0.4",     # Confluence XHTML + Jira wiki 양방향 변환 (R1-R7 보강 포함)
  "platformdirs",
]

[project.optional-dependencies]
keyring = ["keyring>=24"]

[dependency-groups]
dev = [
  "pytest>=8",
  "pytest-asyncio",
  "respx>=0.21",       # httpx mocking
  "syrupy>=4",         # snapshot testing
  "tiktoken",          # 토큰 회계
  "ruff",
  "mypy",
]

[project.scripts]
atls = "atlassian_skills.cli.main:app"
```

---

## 9. 테스트 전략

### 4계층

| 계층 | 비율 | 도구 | 목적 |
|---|---|---|---|
| Unit | ~70% | `pytest` + `respx` | 클라이언트 메서드, 픽스처로 모킹 |
| Contract | ~15% | `pytest` + pydantic | 응답 JSON ↔ 모델 호환성 |
| Snapshot | ~10% | `syrupy` | CLI 출력 텍스트 회귀 (토큰 효율 회귀 방지) |
| Integration | ~5% | `pytest -m integration` | 사내 인스턴스 실호출, CI에선 skip |

### 픽스처 출처

| 제품 | 픽스처 출처 |
|---|---|
| Jira | **`mcp__mcp-atlassian__jira_*` 호출 결과** (Claude가 직접 수집) |
| Confluence | **`mcp__mcp-atlassian__confluence_*` 호출 결과** |
| Bitbucket | Atlassian REST API 1.0 공식 문서 example + 사용자가 던져주는 사내 응답 |
| Bamboo | Atlassian Bamboo REST 공식 example + bamboo-cli 테스트 픽스처 |

### MCP 동등성 검증
Jira/Confluence 명령 각각에 대해:
1. MCP 툴 호출 → 응답 A
2. `atls` 동일 명령 호출 (respx로 모킹된 동일 응답) → 응답 B
3. **B의 정보량 ⊇ MCP가 LLM에 노출하는 핵심 필드**, 토큰 수 ≤ A × 0.5

이 어서션을 거치는 테스트가 자동 메트릭이 됨.

### 토큰 회계
`tests/test_token_budget.py` — 명령별 토큰 상한을 표로 박아두고, 회귀 시 fail.

```python
TOKEN_BUDGETS = {
    "jira issue get": 200,
    "jira issue search (limit=20)": 1500,
    "confluence page get": 800,   # md 본문 포함
    ...
}
```

---

## 10. 릴리즈 게이트 & 마일스톤

> Phase는 시간 경계가 아니라 의존성 경계. 각 릴리즈는 명시적 컷라인을 가진다.

### 0.1.0 — "MCP 완전 대체: Jira/Confluence read+write + RLM workflow" (MVP)
**컷라인**: Jira/Confluence의 read+write 전부 + RFE-001 고수준 명령 + skill 디스패처. 첫 소비자(RLM workflow)가 MCP를 완전히 버릴 수 있는 시점.

**내부 마일스톤** (alpha/rc):
```
0.1.0-alpha.1  Core 골격 + auth + config + Jira read
0.1.0-alpha.2  Confluence read + 토큰 벤치마크 통과
0.1.0-alpha.3  Jira write + Confluence write
0.1.0-alpha.4  RFE-001 고수준 명령 (push-md, pull-md, diff-local)
0.1.0-rc.1     MCP 동등성 검증 + skill 디스패처 + RLM integration test
0.1.0          PyPI publish
```

**포함 (must)** — **부록 A capability matrix의 `0.1.0 = ✅` 행이 단일 source of truth**. 아래 목록은 편의용 요약이며 matrix와 불일치 시 matrix가 우선한다.

**Phase 0 — 골격**:
- [ ] `pyproject.toml`, Typer 엔트리, `core/` (BaseClient, auth §7.1, config, errors, pagination, formatters compact/md/json/raw, Jira 전처리 레이어(§5.1.1), cfxmark wrapper (cfxmark>=0.4, Jira wiki 보강 포함))

**Jira (read 18 + write 31 = 49, Cloud-only 3건 제외 = 46)**:
- [ ] read: `user get`, `issue get`, `issue search`, `issue transitions`, `field search`, `field options`, `project list`, `project issues`, `project versions`, `project components`, `board list`, `board issues`, `sprint list`, `sprint issues`, `link list-types`, `worklog list`, `attachment download`, `watcher list`
- [ ] write: `issue create/update/delete`, `transition`, `comment add/edit/delete`, `worklog add`, `attachment upload/delete`, `link create/remove`, `watcher add/remove`, `sprint create/update/add-issues`, `version create`, `batch_create_issues`, `link_to_epic`, `create_remote_issue_link`
- [ ] Jira Service Desk: `service-desk list`, `queues`, `queue-issues`
- [ ] Jira dev info, dates, SLA, images
- [ ] **Cloud-only 항목 제외** (부록 D): `batch_get_changelogs`, ProForma forms 3종

**Confluence (read 13 + write 10 = 23, Cloud-only 1건 제외 = 22)** — 부록 A.2가 SSOT:
- [ ] read: `page get`, `page search`, `page children`, `space tree`, `comment list`, `label list`, `attachment list`, `user search`, `page history`, `page diff`
- [ ] write: `page create/update/delete/move`, `comment add/reply`, `label add`, `attachment upload/upload-batch/download/download-all/delete`, `page images`
- [ ] **Cloud-only 항목 제외** (부록 D): `page views`

**RFE-001 고수준 명령**:
- [ ] `--passthrough-prefix` (R1) — cfxmark ConversionOptions CLI 노출
- [ ] `confluence page push-md` (R2) — md → canonicalize → PUT + 첨부 업로드
- [ ] `confluence page pull-md` (R3) — GET → md → asset resolve → 파일 기록
- [ ] `--section`, `--heading-promotion`, `--drop-leading-notice` (R4) — Jira wiki 출력 제어
- [ ] `confluence page diff-local` (R5) — 로컬 md vs 서버 canonical 비교
- [ ] `--body-repr=md` 옵션 매트릭스 문서화 (R6)
- [ ] body 미요청 시 REST expand 최소화 (R7)
- [ ] 에러 JSON `context` 필드 (R8)

**본문 변환**:
- [ ] Body conversion round-trip 테스트 (cfxmark `to_jira_wiki` → `from_jira_wiki` 라운드트립, lossy footer 검증)
- [ ] cfxmark R4(sub/sup/ins) 반영 여부에 따른 Jira write lossy warning 범위 확정
- [ ] 코드블록 언어 정규화 매핑 테이블 확정 (cfxmark `code_language_map` 또는 자체 레이어)

**인프라**:
- [ ] 토큰 벤치마크 §5.2 시나리오 1~5 통과 (L1 ≥ 50%, L3 ≥ 60% 절감)
- [ ] `skills/atls/SKILL.md` 디스패처
- [ ] CI: ruff + mypy + pytest + benchmark report
- [ ] PyPI publish

**제외 (explicit, 0.2.0+)**: Bitbucket, Bamboo, 워크플로우 skill 승격, 캐시, async, keyring.

**릴리즈 게이트**:
1. capability matrix Jira/Confluence **read+write** 100% ✅ (Cloud-only 제외)
2. L1 토큰 절감 ≥ 50% (시나리오 1~4 평균)
3. L3 워크플로우 시나리오 ≥ 60% 절감
4. snapshot 테스트 0 회귀
5. integration 테스트 1회 사용자 수동 통과 (사내 인스턴스)
6. **byte-preserving body read 보장** (§15.3): `jira issue get --format=raw`와 `confluence page get --format=raw`가 서버 응답의 body 필드를 **byte-identical**로 반환 (RLM workflow가 MCP를 완전히 버릴 수 있는 전제).
7. **automation contract 구현** (§15): stdin body input, `--dry-run`, exit code 표, `--format=json` 에러 객체, retry-after 가시성.
8. **cfxmark Jira wiki 렌더러 보강 완료**: cfxmark>=0.4 릴리즈에 R1(Table), R2(BlockQuote) 이상 포함. 미완료 시 JiraPreprocessor 포팅으로 fallback (D8 원안 복원).
9. **RLM workflow smoke test**: `workflow.py read/push/diff` 3개 경로가 atls 호출만으로 동작 확인.

### 0.2.0 — "Bitbucket Server + Bamboo"
- [ ] Bitbucket REST API 1.0 엔드포인트 매핑표 (`docs/bitbucket-server-endpoints.md`)
- [ ] Bitbucket read: `project list/get`, `repo list/get/branches/tags/browse`, `pr list/get/diff/commits/activities`, `pr-comment list`, `commit get/diff/comments`
- [ ] Bitbucket write: `pr create/update/approve/unapprove/merge/decline`, `pr-comment add/reply/delete`
- [ ] Bamboo read: `project list/get`, `plan list/get/branches`, `build latest/result/queue`, `deploy projects/environments`, `agent status`
- [ ] Bamboo write: `plan enable/disable`, `build trigger/stop`, `deploy trigger`, `label/comment add`
- [ ] bamboo-cli/cli2 워크플로우 패턴 반영 (`run`, `dry-run`, `vars`)
- [ ] Bitbucket Code Insights API (§16.1)
- [ ] 사용자 사내 응답 픽스처 수집
- [ ] integration 마커 통과

### 0.3.0+ — 워크플로우 skill / 폴리싱
- [ ] `atls-sprint-retro`, `atls-pr-review`, `atls-release-notes` 중 사용 패턴 검증된 것부터
- [ ] async 클라이언트 (옵션)
- [ ] 캐시 (옵션)

---

## 11. references 활용 매핑

| 레포 | 역할 | Phase |
|---|---|---|
| `mcp-atlassian` | Jira/Confluence 명령 사양의 정답지, 픽스처 보강 | 0.1.0 |
| `bitbucket-mcp` (TS, Cloud) | PR 명령 네이밍/UX 참고 | 0.2.0 |
| `bitbucket-mcp2` (Py, Cloud) | "slim response" 가공 패턴 참고 | 0.2.0 |
| `atlassian-python-api` | 엔드포인트/메서드 사전 (의존 X, 컨닝페이퍼) | 0.1.0, 0.2.0 |
| `bamboo-cli` (Go) | Bamboo 핵심 명령 + REST 엔드포인트 추출 | 0.2.0 |
| `bamboo-cli2` (Go) | Bamboo 워크플로우 패턴 (run, dry-run, vars) | 0.2.0 |
| `cfxmark` | Confluence storage/ADF + Jira wiki ↔ md 변환 (런타임 의존). 보강 요구사항: `docs/cfxmark-jira-enhancement-requirements.md` | 0.1.0 |
| `atlassian-cli` (Rust) | CLI 명령 트리/네이밍 비교 참고 | 0.1.0 |
| `atlassian-cli2` (Go) | STANDARDS.md, 명령 컨벤션 참고 | 0.1.0 |
| `atlassian-cli3` (Zig) | 스킵 | — |

---

## 12. 라이선스
**MIT.** 단순하고 채택률 높음. cfxmark와도 정렬.

## 13. 추가 결정 사항 (Resolved)

### D13. Bitbucket 응답 슬리밍 default 필드
원칙: PR 워크플로우 중심으로 좁게. `--fields=all`로 raw 접근.

| 리소스 | default 필드 |
|---|---|
| `bb pr list` | `id, state, title, author, source→target, updated, reviewers(approved 수)` |
| `bb pr get` | 위 + `description(md), commits 수, +N/-M lines, open tasks 수` |
| `bb pr diff` | 본문 그대로, `--max-lines=200` 기본 컷 |
| `bb repo list` | `slug, name, default_branch, updated` |
| `bb commit get` | `hash[:8], author, date, message 첫 줄` |

### D14. 인증 — env first, keyring optional
- **default**: `ATLS_<PROFILE>_<PRODUCT>_TOKEN` 환경변수 (예: `ATLS_CORP_JIRA_TOKEN`).
- **opt-in**: `pip install atlassian-skills[keyring]` 후 `auth = "keyring"`로 활성화.
- **plaintext**: 명시적 `auth = "plaintext"`로만 허용. 절대 default 아님.
- 우선순위: CLI 플래그 > env > keyring > config 평문.

### D15. 캐시 — 도입 안 함
- stale 버그 회피, LLM 세션 짧아 적중률 낮음.
- `--cache` 플래그 자리만 인터페이스에 남겨두고 구현 X.
- Phase 4 이후 사용 패턴 보고 재검토.

### D16. async — sync only (MVP)
- CLI는 sync로 충분. `httpx` sync 모드 사용.
- async는 backlog. 라이브러리 사용자가 생기면 평행 트랙으로 `async_client` 추가.

### D17. 워크플로우 skill — dispatcher 1개만
- MVP는 `skills/atls/SKILL.md` 디스패처 하나.
- "Claude가 같은 명령 조합을 3번 이상 재발명" 보일 때만 skill로 승격.
- **CLI 복합 명령은 별도** (RFE-001): `push-md`, `pull-md`, `diff-local` 등 cfxmark 래핑 명령은 skill이 아닌 CLI 서브커맨드로 추가. "3번 재발명" 조건은 skill 승격에만 적용.
- 후보 backlog (구현 X, 메모만):
  - `atls-sprint-retro` — board → active sprint → done issues → Confluence page 생성
  - `atls-pr-review` — PR + diff + 관련 Jira + Bamboo 빌드 상태
  - `atls-release-notes` — fix version → 이슈 목록 → Confluence page

## 14. 미정 / 후속 결정 필요
(현재 없음 — 발견되는 대로 추가)

---

## 15. Automation Contract

> atls는 "사람용 CLI"가 아니라 **에이전트가 호출하는 실행 경로**이다. 자동화 친화성은 기능 1개급 우선순위로 취급하며, 본 섹션은 모든 명령이 지켜야 하는 공통 계약을 정의한다.

### 15.1 Stdin body input (모든 write 명령)

모든 write 명령은 body 입력을 stdin으로 받을 수 있어야 한다.

- `--body-file=-`: stdin으로부터 body 읽기 (canonical form)
- `--body-file=<path>`: 파일에서 읽기
- `--body=<inline>`: 인라인 문자열 (짧은 경우만, 쉘 escape 주의)

적용 명령: `jira issue create/update`, `jira comment add/edit`, `confluence page create/update`, `confluence comment add/reply` 등.

예:
```
cat page.storage.xml | atls confluence page update <id> \
  --body-format=storage --body-file=- --if-version=42
```

### 15.2 Dry-run 및 optimistic concurrency

- `--dry-run`: 네트워크 호출 없이 **전송할 payload**(method, URL, headers 요약, body)만 `--format` 규칙에 따라 출력. exit 0.
- `confluence page update --if-version=<n>`: Confluence의 낙관적 동시성. 서버 버전 ≠ n이면 HTTP 409 → exit 5 (stale).
- `jira issue update --if-updated=<iso8601>`: Jira는 precondition 헤더가 제한적이므로 client-side check: `get` → compare `updated` → PUT. 불일치 시 exit 5.
- 모든 write는 "변경 없음 감지 시 no-op + exit 0" 정책(workflow.py push 호환).

### 15.3 Byte-preserving body read 보장 (0.1.0 필수)

RLM workflow는 MCP가 Jira wiki markup을 silent drop하는 문제로 MCP 본문 조회를 금지한 상태다. atls는 이 gap을 메워야 MCP 대체가 가능하다.

**보장사항**:
- `jira issue get <KEY> --format=raw`: `fields.description`, `fields.comment.comments[].body`가 **서버 응답 문자열 그대로** (Jira wiki markup, 특수문자 `~ + - * {} []` 보존, 어떤 escape/unescape도 금지).
- `confluence page get <ID> --format=raw`: `body.storage.value`를 **byte-identical**로 반환. cfxmark 경유 금지.
- 출력 envelope가 JSON이면 body 필드는 원본 string을 그대로 JSON escape만 해서 실림. `--format=raw` 단일 모드에선 envelope 없이 순수 bytes.
- 회귀 방지: `tests/test_body_preservation.py`에 RLM이 실제로 터졌던 케이스(예: "2~3 스프린트" → MCP는 "23 스프린트"로 drop) fixture를 박아두고 snapshot 비교.

**추가 플래그**:
- `jira issue get --body-repr=wiki|md|raw` — body 표현만 선택적으로 전환 (envelope는 compact/json 유지).
- `confluence page get --body-repr=storage|md|raw` — 동일.
- `--body-repr=raw`는 `--format=raw`와 동치.

**`--body-repr` 별 cfxmark 옵션 유효성 매트릭스** (RFE-001 R6):

| 플래그 | `--body-repr=md` | `--body-repr=raw` | `--body-repr=wiki` |
|---|---|---|---|
| `--passthrough-prefix` | 적용 | 무시 | 무시 |
| `--resolve-assets` | 적용 (sidecar/inline/none) | 무시 | 무시 |
| `--heading-promotion` | N/A (Confluence) | N/A | 적용 (Jira wiki output) |
| `--section` | N/A | N/A | 적용 |

### 15.4 Exit code 규약

| code | 의미 | 예 |
|---|---|---|
| 0 | OK | 성공, no-op 포함 |
| 1 | generic error | 분류 불가 |
| 2 | not found | 404, 존재하지 않는 key/id |
| 3 | permission denied | 403 |
| 4 | conflict | 409 (이미 존재 등) |
| 5 | stale / precondition failed | `--if-version` 불일치, 412 |
| 6 | auth | 401, 토큰 누락/만료 |
| 7 | validation | 400, 필드/JQL 문법 에러 |
| 10 | network | DNS/connection/timeout |
| 11 | rate limited | 429 (retry 소진 후) |

모든 exit code는 `atls --explain-exit=<N>`으로 설명 가능.

**예외**: `confluence page diff-local` 명령은 Unix diff 전통에 따라 exit 1 = "차이 있음" (에러 아님). 이 명령에서 generic error는 exit 1이 아닌 다른 코드로 분기.

### 15.5 Machine-readable error (`--format=json`)

에러도 JSON envelope로 stdout에 출력(성공과 동일 스트림). stderr는 human-readable 로그만.

```json
{
  "error": {
    "code": "CONFLICT_409",
    "exit": 4,
    "message": "Confluence page version 42 does not match server version 43",
    "hint": "atls confluence page get <id> --fields=version 로 최신 버전 확인 후 재시도",
    "http": {"status": 409, "url": "...", "method": "PUT"},
    "context": {
      "local_version": 42,
      "server_version": 43
    }
  }
}
```

- `code`: 내부 enum (stable)
- `exit`: §15.4 표의 숫자
- `context`: 에러 유형별 추가 정보 (RFE-001 R8). best-effort enrichment — 없을 수 있으므로 caller는 `message`만으로도 동작해야 함. 예: conflict → version 쌍, stale → timestamp 쌍.
- `hint`: **agent 자가복구용 추천 명령**. 없을 수 있음.
- `http`: 원인이 HTTP 응답인 경우만.

compact/md 모드에서도 `--format=json` override 없이 에러 객체의 `message`와 `hint`는 stderr로 출력.

### 15.6 Rate-limit / retry-after 가시성

- 429/5xx 재시도는 내부에서 지수 백오프로 처리 (BaseClient 책임).
- **기본 동작**: 재시도가 1회라도 발생하면 stderr에 1줄 warning: `[atls] retry 1/3 after 2.3s (429 rate-limited, retry-after=2s)`.
- `--quiet`로 suppress, `-vv`로 전체 요청 로그. **`-vv` 로그에서 `Authorization` 헤더는 `Bearer ***` / `Basic ***`로 마스킹**.
- 재시도 소진 시 exit 11 + §15.5 에러 객체.

### 15.7 Customfield 쓰기 계약

Jira Epic Link 등 customfield 의존 워크플로우를 위해 explicit write 경로를 제공.

- `jira field search --name "Epic Link"` → `{"id": "customfield_10014", "name": "Epic Link", "type": "any"}` (id 해석 1차)
- `jira issue update <KEY> --set-customfield customfield_10014=RLM-8`
- 반복 사용: `--set-customfield id1=v1 --set-customfield id2=v2`
- 타입 추론은 하지 않음(에이전트가 field 메타 확인 후 명시). `--set-customfield-json id=<json>`로 복합 값 지원.
- 근거: RLM `_lib.py:329-377`의 Epic Link 실패 케이스(MCP `link_to_epic`이 customfield 쓰지 못함).

### 15.8 Sub-resource expansion

호출 횟수 절감을 위해 `jira issue get`에 include 플래그 도입.

- `jira issue get <KEY> --include=comments:3,attachments,transitions,worklog:5`
- `compact`/`md`: 토큰 예산 초과 시 자동 truncate + footer 표시
- `json`/`raw`: 구조 보존, truncate 없음
- 0.1.x 또는 0.2 초반 구현 (0.1.0 블로커 아님).

### 15.9 Runtime token budget guard (optional, 0.2+)

- `atls ... --max-tokens=2000`: compact/md 모드에서만 적용. 초과 시 truncate + `[truncated: 120 tokens cut]` footer.
- raw/json은 **truncate 금지** (의미 파괴). 초과 시 exit 1 + hint "use --format=raw and paginate".

---

## 16. Bitbucket PR Automation Backlog (0.3.0)

§D13 응답 슬리밍은 표시 계약이고, 본 섹션은 **실제 PR 워크플로우**(rlm-review, auto-merge, code insights) 계약이다.

### 16.1 Code Insights API — 1급 시민

`tools/review/post_pass.py` 같은 dumb wrapper가 호출할 수 있어야 함.

```
bb insights report put <commit-sha> \
    --key rlm-review --title "RLM Review" \
    --result PASS|FAIL|PENDING \
    --details "..." --link <url> \
    --data-file=-

bb insights report get <commit-sha> --key rlm-review
bb insights report list <commit-sha>
bb insights annotation add <commit-sha> --report-key rlm-review \
    --path src/foo.py --line 42 --severity HIGH --message "..."
bb insights annotation list <commit-sha> --report-key rlm-review
```

- `--key rlm-review`는 예시. RLM-specific default는 넣지 않는다.

### 16.2 `bb pr create` — gh 동등 UX

```
bb pr create \
    --from-branch feature/x --to-branch develop \
    --title "..." --description-file=- \
    --reviewers alice,bob --draft=false \
    --enable-auto-merge \
    --describe-from-commits
```

- `--describe-from-commits`: 최근 커밋 메시지를 병합해 description 초안 생성 (local git 필요).
- `--enable-auto-merge`: Bitbucket REST가 지원하면 활성화, 미지원 버전이면 PR URL 출력 + stderr 경고 "auto-merge unsupported on this server; enable via UI".
- stdout 마지막 줄: PR URL (스크립팅 친화).
- RLM-specific default (`--to-branch=develop` 등)는 core default 금지, `atls config`의 per-repo override로.

### 16.3 `bb pr checks` — merge-check 집계

```
bb pr checks <pr-id>
# compact 출력 예:
# auto-merge=PENDING build=GREEN insights=MISSING(rlm-review) reviewers=0/1 tasks=0/0
```

- build/insights/reviewers/tasks 상태를 한 줄 요약 + `--format=json`으로 full detail.

### 16.4 Diff 필터

```
bb pr diff <pr-id> --context=3 \
    --path-glob="src/**" --exclude="*.lock" \
    --max-lines=200
bb commit diff <sha1>..<sha2> --path-glob=... --max-lines=...
```

- `--paginate`: 0.3.x 후기 — 2차 우선.

### 16.5 기본 repo 컨텍스트 auto-detect

- 현재 디렉토리의 `git config remote.origin.url` 파싱 → `project/repo` 추론.
- `atls config set-default-repo <project>/<repo>`로 명시 override.
- 모든 `bb` 명령의 `--repo` 생략 허용.

### 16.6 (선택) branch permission / default reviewer read

- `bb repo branch-permissions list <repo>`
- `bb repo default-reviewers list <repo>`
- 0.3.1+ read-only admin helper. 우선순위 낮음.

---

## 부록 A. MCP ↔ atls Capability Matrix

> 출처: `references/mcp-atlassian/docs/tools-reference.mdx` (Jira 49 + Confluence 23 = **72 tools total**).
> 컬럼: `MCP tool name` → `atls 명령` → `read/write` → `0.1.0` → `비고`.
> 0.1.0 = Jira/Confluence read+write 전부 (MCP 완전 대체). 0.2.0 = Bitbucket/Bamboo.
> 범례: `✅` 0.1.0 포함 / `☁️` Cloud-only → Server/DC 전용 타겟에서 제외 (부록 D).
> **본 matrix가 릴리즈 scope의 단일 source of truth**. §10 마일스톤 목록과 충돌 시 matrix가 우선한다.

### A.1 Jira (49)

| # | MCP tool | atls 명령 | R/W | 0.1.0 | 비고 |
|---|---|---|---|---|---|
| 1 | get_user_profile | `jira user get` | R | ✅ | |
| 2 | get_issue | `jira issue get` | R | ✅ | default fields 프루닝 |
| 3 | search | `jira issue search` | R | ✅ | JQL |
| 4 | search_fields | `jira field search` | R | ✅ | |
| 5 | get_field_options | `jira field options` | R | ✅ | |
| 6 | get_project_issues | `jira project issues` | R | ✅ | |
| 7 | get_transitions | `jira issue transitions` | R | ✅ | |
| 8 | get_worklog | `jira worklog list` | R | ✅ | |
| 9 | download_attachments | `jira attachment download` | R | ✅ | 파일 IO만 |
| 10 | get_issue_images | `jira issue images` | R | ✅ | |
| 11 | get_agile_boards | `jira board list` | R | ✅ | |
| 12 | get_board_issues | `jira board issues` | R | ✅ | |
| 13 | get_sprints_from_board | `jira sprint list` | R | ✅ | |
| 14 | get_sprint_issues | `jira sprint issues` | R | ✅ | |
| 15 | get_link_types | `jira link list-types` | R | ✅ | |
| 16 | get_all_projects | `jira project list` | R | ✅ | |
| 17 | get_project_versions | `jira project versions` | R | ✅ | |
| 18 | get_project_components | `jira project components` | R | ✅ | |
| 19 | get_issue_dates | `jira issue dates` | R | ✅ | |
| 20 | get_issue_sla | `jira issue sla` | R | ✅ | JSM 의존 |
| 21 | get_issue_development_info | `jira dev-info get` | R | ✅ | |
| 22 | get_issues_development_info | `jira dev-info get-many` | R | ✅ | |
| 23 | get_issue_watchers | `jira watcher list` | R | ✅ | |
| 24 | get_issue_proforma_forms | `jira form list` | R | ☁️ | Cloud-only (부록 D) |
| 25 | get_proforma_form_details | `jira form get` | R | ☁️ | Cloud-only (부록 D) |
| 26 | get_service_desk_for_project | `jira service-desk list` | R | ✅ | |
| 27 | get_service_desk_queues | `jira service-desk queues` | R | ✅ | |
| 28 | get_queue_issues | `jira service-desk queue-issues` | R | ✅ | |
| 29 | batch_get_changelogs | `jira issue-batch get-changelogs` | R | ☁️ | Cloud-only (부록 D) |
| 30 | create_issue | `jira issue create` | W | ✅ | |
| 31 | batch_create_issues | `jira issue-batch create` | W | ✅ | |
| 32 | update_issue | `jira issue update` | W | ✅ | body 보존 §5.1 |
| 33 | delete_issue | `jira issue delete` | W | ✅ | |
| 34 | add_comment | `jira comment add` | W | ✅ | |
| 35 | edit_comment | `jira comment edit` | W | ✅ | |
| 36 | add_worklog | `jira worklog add` | W | ✅ | |
| 37 | link_to_epic | `jira epic link` | W | ✅ | |
| 38 | create_issue_link | `jira link create` | W | ✅ | |
| 39 | create_remote_issue_link | `jira link remote-create` | W | ✅ | |
| 40 | remove_issue_link | `jira link delete` | W | ✅ | |
| 41 | transition_issue | `jira issue transition` | W | ✅ | |
| 42 | add_watcher | `jira watcher add` | W | ✅ | |
| 43 | remove_watcher | `jira watcher remove` | W | ✅ | |
| 44 | create_sprint | `jira sprint create` | W | ✅ | |
| 45 | update_sprint | `jira sprint update` | W | ✅ | |
| 46 | add_issues_to_sprint | `jira sprint add-issues` | W | ✅ | |
| 47 | create_version | `jira project versions create` | W | ✅ | |
| 48 | batch_create_versions | `jira project versions-batch` | W | ✅ | |
| 49 | update_proforma_form_answers | `jira form answer` | W | ☁️ | Cloud-only (부록 D) |

**Jira 합계**: 49건 중 Cloud-only 4건 제외 → **0.1.0 구현 대상 45건** (R 22 + W 23).

### A.2 Confluence (23 + 1)

| # | MCP tool | atls 명령 | R/W | 0.1.0 | 비고 |
|---|---|---|---|---|---|
| 1 | search | `confluence page search` | R | ✅ | CQL |
| 2 | get_page | `confluence page get` | R | ✅ | body=cfxmark md |
| 3 | get_page_children | `confluence page children` | R | ✅ | |
| 4 | get_space_page_tree | `confluence space tree` | R | ✅ | |
| 5 | get_comments | `confluence comment list` | R | ✅ | |
| 6 | get_labels | `confluence label list` | R | ✅ | |
| 7 | search_user | `confluence user search` | R | ✅ | |
| 8 | get_page_history | `confluence page history` | R | ✅ | |
| 9 | get_page_diff | `confluence page diff` | R | ✅ | |
| 10 | get_page_views | `confluence page views` | R | ☁️ | Cloud-only (부록 D) |
| 11 | get_attachments | `confluence attachment list` | R | ✅ | |
| 12 | download_attachment | `confluence attachment download` | R | ✅ | |
| 13 | download_content_attachments | `confluence attachment download-all` | R | ✅ | |
| 14 | get_page_images | `confluence page images` | R | ✅ | |
| 15 | add_label | `confluence label add` | W | ✅ | |
| 16 | create_page | `confluence page create` | W | ✅ | body 보존 |
| 17 | update_page | `confluence page update` | W | ✅ | body 보존 |
| 18 | delete_page | `confluence page delete` | W | ✅ | |
| 19 | move_page | `confluence page move` | W | ✅ | |
| 20 | add_comment | `confluence comment add` | W | ✅ | |
| 21 | reply_to_comment | `confluence comment reply` | W | ✅ | |
| 22 | upload_attachment | `confluence attachment upload` | W | ✅ | |
| 23 | upload_attachments | `confluence attachment upload-batch` | W | ✅ | |
| +1 | delete_attachment | `confluence attachment delete` | W | ✅ | tools-reference 누락, 구현 포함 |

**Confluence 합계**: 23+1건 중 Cloud-only 1건 제외 → **0.1.0 구현 대상 23건** (R 13 + W 10).

### A.3 atls-only 고수준 명령 (MCP 대응 없음, RFE-001)

| # | atls 명령 | R/W | 0.1.0 | 비고 |
|---|---|---|---|---|
| 1 | `confluence page push-md` | W | ✅ | md → canonicalize → PUT + 첨부 (RFE-001 R2) |
| 2 | `confluence page pull-md` | R | ✅ | GET → md → asset resolve → 파일 기록 (RFE-001 R3) |
| 3 | `confluence page diff-local` | R | ✅ | 로컬 md vs 서버 canonical 비교 (RFE-001 R5) |

**총 합계**: Jira 49 + Confluence 24 = **73**. Cloud-only 5건 제외 → **0.1.0 구현 대상 68건** + atls-only 3건 = **71건**.

### A.4 Bitbucket Server / Bamboo
0.2.0에서 별도 부록 B/C로 추가. Bitbucket은 `references/bitbucket-mcp` 49 tools 중 Server 1.0에서 동작 가능한 항목만 포함하고, Cloud-only(Pipelines, Branching Model)는 제외.

---

## 부록 D. Cloud-only 항목 (Server/DC 타겟에서 제외)

> 근거: `references/mcp-atlassian/docs/compatibility.mdx:121-124`. 이 도구들은 mcp-atlassian에서 Cloud-only로 명시되어 있고, Server/DC API에 대응 엔드포인트가 없거나 결과가 비호환이다. 본 프로젝트는 Server/DC 전용 목표(§1 목표 4)에 따라 이 항목들을 **구현 대상에서 명시적으로 제외**한다.

| MCP tool | 사유 | 재검토 조건 |
|---|---|---|
| `batch_get_changelogs` | Server/DC는 `/rest/api/2/issue/{key}?expand=changelog`를 per-issue로만 지원. 배치 엔드포인트 부재. | Server/DC 측 신규 API 추가 시 |
| `get_issue_proforma_forms` | ProForma Forms API는 Cloud 전용. Server/DC용 "Forms for Jira" 앱은 별도 REST 스키마를 가지며 MCP tool과 비호환. | Server용 Forms REST를 별도 매핑할 때 |
| `get_proforma_form_details` | 위와 동일 | 위와 동일 |
| `update_proforma_form_answers` | 위와 동일 (write) | 위와 동일 |
| `get_page_views` | Confluence Cloud Analytics API에만 존재. Server/DC는 Analytics 플러그인이 있어야 하고 엔드포인트/스키마가 다름. | Analytics 플러그인 타겟 결정 시 |

matrix의 `☁️` 마커는 본 부록을 가리킨다. 향후 Cloud 호환을 목표에 추가하면 이 부록을 삭제하고 matrix 행을 `✅`로 되돌린다.

---

## 부록 E. RLM Migration Compatibility (first adopter contract)

> atls의 첫 실제 소비자는 사내 RLM workflow다. 본 부록은 RLM `tools/workflow/` 및 `workflow_agent.md`가 MCP → atls로 이전할 때 쓰는 **1:1 명령 매핑 + 통과해야 할 동작 계약**을 정의한다. RLM-specific default는 atls core에 박지 않고 본 부록에만 예시로 둔다.

### E.1 배경
- 현재 RLM은 Jira wiki 본문 조회를 MCP에서 금지하고 있다 (근거: `workflow_agent.md:62`, MCP가 `~ + * {}` 등 특수문자를 silent drop).
- Story canonical form = Confluence storage XHTML, Epic canonical form = Jira wiki markup (`workflow_agent.md:156-166`).
- Epic Link 세팅은 MCP `link_to_epic`으로 실패한 이력이 있음 (`_lib.py:329-377`, RLM-15/16 장애).
- 첨부 push는 basename 기반 멱등성 (`workflow_agent.md:194-199, :516`).

### E.2 1:1 명령 매핑

| RLM 내부 호출 (현재 MCP) | atls 등가 명령 | 의존 계약 |
|---|---|---|
| `jira_get_issue(KEY, expand="comments")` | `jira issue get <KEY> --include=comments:N --format=raw` | §15.3 byte-preserving, §15.8 include |
| `jira_create_issue(type=Story, ...)` | `jira issue create --type=Story --project=RLM --summary=... --body-format=wiki --body-file=-` | §15.1 stdin, §5.1 write |
| `jira_update_issue(KEY, fields=...)` | `jira issue update <KEY> --set-customfield customfield_10014=<EPIC-KEY> --body-format=wiki --body-file=-` | §15.7 customfield, §15.2 dry-run |
| `jira_link_to_epic(KEY, epic)` | `jira issue update <KEY> --set-customfield <epic-link-id>=<epic>` (Epic Link customfield 직접 쓰기) | §15.7 (MCP 버전 사용 금지) |
| `confluence_get_page(ID)` | `confluence page get <ID> --body-repr=storage --format=raw` | §15.3 byte-preserving |
| `confluence_create_page(space, parent, title, body)` | `confluence page create --space=RLM --parent=<hub-id> --title=... --body-format=storage --body-file=-` | §15.1 stdin |
| `confluence_update_page(ID, version, body)` | `confluence page update <ID> --if-version=<n> --body-format=storage --body-file=-` | §15.2 optimistic concurrency |
| `confluence_upload_attachments(page, paths)` | `confluence attachment upload-batch <page-id> <paths...> --if-exists=skip` | §E.3 basename 규약 |
| `jira_create_remote_issue_link(KEY, url, title)` | `jira link remote-create <KEY> --url=... --title=...` | — |

### E.3 Attachment basename 규약

RLM push는 `docs/jira/<KEY>/assets/<file>.png`를 basename으로 업로드하고, 동일 basename이면 skip한다 (`workflow_agent.md:516`).

- `confluence attachment upload-batch` default: `--if-exists=skip`
- 옵션: `replace`(기존 attachment 최신 버전으로 교체), `version`(새 버전으로 추가)
- basename 충돌 판정은 서버측 `title` 필드 비교. 확장자 포함 전체 basename.
- `jira attachment upload`도 동일한 `--if-exists` 플래그 노출.

### E.4 MCP fallback 금지 정책

- `skills/atls/SKILL.md`에 명시: **본 skill이 loaded되면 `mcp__mcp-atlassian__*` 호출 금지**. 대안 표는 본 부록 E.2 참조.
- DESIGN.md 수준의 SSOT는 본 부록이며, 실제 enforcement는 skill 파일.
- 예외: **어떤 이유로도 atls가 커버하지 못하는 Cloud-only 경로** (부록 D 항목) — 이 경우도 RLM은 호출하지 않음(RLM이 Server 전용이므로 해당 없음).

### E.5 RLM migration 통과 기준 (0.1.0 릴리즈 게이트 부가 조건)

- [ ] §E.2 read 경로 전부 동작, byte-preserving 테스트 통과
- [ ] §15 automation contract 전부 구현 (stdin, dry-run, exit codes, json errors, retry visibility)
- [ ] RLM workflow.py가 MCP 호출 0건으로 end-to-end 성공 (사용자 수동 검증 1회)
- [ ] `tests/integration/rlm_parity/` 시나리오 5개 green (issue get, page get, page update with version conflict, attachment upload idempotent, customfield epic link set)
