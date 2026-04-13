# REST API 엔드포인트 매핑표

> DESIGN.md 부록 A(MCP tool → atls 명령)를 보완하여, **atls 명령 → REST endpoint** 매핑을 정의한다.
> 구현 시 각 명령이 어떤 엔드포인트를 호출해야 하는지의 단일 참조 문서.
> 0.1.0 = Jira/Confluence read+write 전부 (부록 A matrix가 SSOT).

상태: **Draft v2** — 2026-04-13
출처: `references/mcp-atlassian` 소스 분석 + `references/atlassian-python-api` 교차 참조

---

## 1. 공통 사항

### 1.1 Base URL
- Jira: `{base}/rest/api/2/...` (Server/DC)
- Jira Agile: `{base}/rest/agile/1.0/...`
- Jira Service Desk: `{base}/rest/servicedeskapi/...`
- Confluence: `{base}/rest/api/content/...` (Server/DC v1 API)

> Cloud는 비목표(DESIGN.md §1). Cloud v2/v3 API 경로는 참고용으로만 기록.

### 1.2 인증
- **PAT (Bearer)**: `Authorization: Bearer <token>` — 기본
- **Basic**: `Authorization: Basic base64(user:token)` — opt-in (§7.1)

### 1.3 페이지네이션 패턴

| 패턴 | 사용처 | 파라미터 | 응답 필드 |
|---|---|---|---|
| **offset** | Jira search, boards, sprints | `startAt` (0-based), `maxResults` | `total`, `startAt`, `maxResults` |
| **link-follow** | Confluence 대부분 | `start`, `limit` | `_links.next` 존재 여부로 다음 페이지 판단 |
| **none** | 단건 조회, link types, transitions | — | — |

### 1.4 Write 응답 패턴

| 패턴 | HTTP 상태 | 응답 본문 | 사용처 |
|---|---|---|---|
| **created** | 201 | 생성된 리소스 전체 JSON | issue create, page create, comment add |
| **updated** | 200 | 업데이트된 리소스 전체 JSON | issue update, page update |
| **no-content** | 204 | (없음) | transition, delete, watcher add/remove, sprint add-issues |
| **multipart** | 200 | 첨부 메타데이터 JSON | attachment upload |

---

## 2. Jira — Read (22건)

### 2.1 Issue

| atls 명령 | HTTP | Endpoint | Query Parameters | 비고 |
|---|---|---|---|---|
| `jira user get <id>` | GET | `/rest/api/2/user?username={id}` | — | Server: `username`, Cloud: `accountId` |
| `jira issue get <key>` | GET | `/rest/api/2/issue/{key}` | `fields`, `expand`, `comment_limit`→내부 처리, `properties` | default fields 프루닝 |
| `jira issue search <jql>` | GET | `/rest/api/2/search` | `jql`, `fields`, `startAt`, `maxResults`, `expand` | offset 페이지네이션. `maxResults` ≤ 50 |
| `jira issue transitions <key>` | GET | `/rest/api/2/issue/{key}/transitions` | — | 응답: `transitions[]` with `id`, `name`, `to` |
| `jira issue images <key>` | GET | `/rest/api/2/issue/{key}?fields=attachment` | `fields=attachment` | 이미지 첨부만 필터 (client-side) |
| `jira issue dates <key>` | GET | `/rest/api/2/issue/{key}?fields=duedate,created,updated,resolutiondate` | 날짜 필드만 | |
| `jira issue sla <key>` | GET | `/rest/servicedeskapi/request/{key}/sla` | — | JSM 의존. Service Desk API |

### 2.2 Field

| atls 명령 | HTTP | Endpoint | Query Parameters | 비고 |
|---|---|---|---|---|
| `jira field search [keyword]` | GET | `/rest/api/2/field` | — | 전체 fetch → **client-side fuzzy match** |
| `jira field options <field_id>` | GET | `/rest/api/2/issue/createmeta` | `projectKeys`, `issuetypeNames`, `expand=projects.issuetypes.fields` | Server: createmeta에서 allowedValues 추출 |

### 2.3 Project

| atls 명령 | HTTP | Endpoint | Query Parameters | 비고 |
|---|---|---|---|---|
| `jira project list` | GET | `/rest/api/2/project` | `includeArchived` | 페이지네이션 없음 |
| `jira project issues <key>` | GET | `/rest/api/2/search` | `jql=project="{key}"`, `fields`, `startAt`, `maxResults` | search 재사용 |
| `jira project versions <key>` | GET | `/rest/api/2/project/{key}/versions` | `expand` | 페이지네이션 없음 |
| `jira project components <key>` | GET | `/rest/api/2/project/{key}/components` | — | 페이지네이션 없음 |

### 2.4 Board / Sprint (Agile API)

| atls 명령 | HTTP | Endpoint | Query Parameters | 비고 |
|---|---|---|---|---|
| `jira board list` | GET | `/rest/agile/1.0/board` | `name`, `projectKeyOrId`, `type`, `startAt`, `maxResults` | offset |
| `jira board issues <id>` | GET | `/rest/agile/1.0/board/{id}/issue` | `jql`, `fields`, `startAt`, `maxResults` | |
| `jira sprint list <board_id>` | GET | `/rest/agile/1.0/board/{boardId}/sprint` | `state`, `startAt`, `maxResults` | offset |
| `jira sprint issues <sprint_id>` | GET | `/rest/api/2/search` | `jql=sprint={sprintId}`, `fields`, `startAt`, `maxResults` | search 재사용 |

### 2.5 Dev Info / Service Desk / 기타

| atls 명령 | HTTP | Endpoint | Query Parameters | 비고 |
|---|---|---|---|---|
| `jira dev-info get <key>` | GET | `/rest/dev-status/1.0/issue/detail?issueId={id}&applicationType=...&dataType=...` | `issueId`, `applicationType`, `dataType` | dev-status API |
| `jira dev-info get-many <keys>` | GET | `/rest/dev-status/1.0/issue/summary?issueId=...` | `issueId` (반복) | 복수 이슈 |
| `jira service-desk list` | GET | `/rest/servicedeskapi/servicedesk` | — | JSM API |
| `jira service-desk queues <sd_id>` | GET | `/rest/servicedeskapi/servicedesk/{id}/queue` | — | |
| `jira service-desk queue-issues <queue_id>` | GET | `/rest/servicedeskapi/servicedesk/{sd}/queue/{q}/issue` | `startAt`, `maxResults` | offset |
| `jira link list-types` | GET | `/rest/api/2/issueLinkType` | — | |
| `jira worklog list <key>` | GET | `/rest/api/2/issue/{key}/worklog` | — | |
| `jira attachment download <key>` | GET | `/rest/api/2/issue/{key}?fields=attachment` → content URL | — | 2단계 |
| `jira watcher list <key>` | GET | `/rest/api/2/issue/{key}/watchers` | — | |

---

## 3. Jira — Write (23건)

### 3.1 Issue CRUD

| atls 명령 | HTTP | Endpoint | Request Body | Response | 비고 |
|---|---|---|---|---|---|
| `jira issue create` | POST | `/rest/api/2/issue` | `{fields: {project, summary, issuetype, description, assignee, ...}}` | `{id, key, self}` | |
| `jira issue-batch create` | POST | `/rest/api/2/issue/bulk` | `{issues: [{fields: ...}, ...]}` | `{issues: [{id, key}], errors: []}` | |
| `jira issue update <key>` | PUT | `/rest/api/2/issue/{key}` | `{fields: {...}, update: {...}}` | 200 (업데이트된 이슈) | body 보존 §5.1 |
| `jira issue delete <key>` | DELETE | `/rest/api/2/issue/{key}` | — | 204 No Content | |
| `jira issue transition <key>` | POST | `/rest/api/2/issue/{key}/transitions` | `{transition: {id}, fields: {...}, comment: ...}` | 204 No Content | |

### 3.2 Comment / Worklog

| atls 명령 | HTTP | Endpoint | Request Body | Response | 비고 |
|---|---|---|---|---|---|
| `jira comment add <key>` | POST | `/rest/api/2/issue/{key}/comment` | `{body: "..."}`, opt `visibility` | `{id, body, author, created}` | |
| `jira comment edit <key> <id>` | PUT | `/rest/api/2/issue/{key}/comment/{id}` | `{body: "..."}` | `{id, body, author, updated}` | |
| `jira worklog add <key>` | POST | `/rest/api/2/issue/{key}/worklog` | `{timeSpentSeconds, comment, started}` | `{id, timeSpent, author}` | `?adjustEstimate=new&newEstimate=...` |

### 3.3 Link

| atls 명령 | HTTP | Endpoint | Request Body | Response | 비고 |
|---|---|---|---|---|---|
| `jira link create` | POST | `/rest/api/2/issueLink` | `{type: {name}, inwardIssue: {key}, outwardIssue: {key}}` | `{id, type, inwardIssue, outwardIssue}` | |
| `jira link remote-create <key>` | POST | `/rest/api/2/issue/{key}/remotelink` | `{object: {url, title}, relationship}` | `{id, self}` | |
| `jira link delete <id>` | DELETE | `/rest/api/2/issueLink/{id}` | — | 204 No Content | |
| `jira epic link <key> <epic>` | PUT | `/rest/api/2/issue/{key}` | `{fields: {customfield_<epicLinkId>: "<epic>"}}` | 200 | update_issue 재사용 |

### 3.4 Watcher

| atls 명령 | HTTP | Endpoint | Request Body | Response | 비고 |
|---|---|---|---|---|---|
| `jira watcher add <key> <user>` | POST | `/rest/api/2/issue/{key}/watchers` | `"username"` (plain string) | 204 No Content | Server: username |
| `jira watcher remove <key> <user>` | DELETE | `/rest/api/2/issue/{key}/watchers?username={user}` | — | 204 No Content | |

### 3.5 Sprint

| atls 명령 | HTTP | Endpoint | Request Body | Response | 비고 |
|---|---|---|---|---|---|
| `jira sprint create` | POST | `/rest/agile/1.0/sprint` | `{name, boardId, startDate, endDate, goal}` | `{id, name, state, ...}` | |
| `jira sprint update <id>` | PUT | `/rest/agile/1.0/sprint/{id}` | `{name, state, startDate, endDate, goal}` | 업데이트된 sprint | |
| `jira sprint add-issues <id>` | POST | `/rest/agile/1.0/sprint/{id}/issue` | `{issues: ["KEY-1", "KEY-2"]}` | 204 No Content | |

### 3.6 Version

| atls 명령 | HTTP | Endpoint | Request Body | Response | 비고 |
|---|---|---|---|---|---|
| `jira project versions create` | POST | `/rest/api/2/version` | `{project, name, startDate, releaseDate, description}` | `{id, name, ...}` | |
| `jira project versions-batch` | POST × N | `/rest/api/2/version` (반복) | 동일 | 동일 | 순차 호출 |

---

## 4. Confluence — Read (11건)

### 4.1 Page

| atls 명령 | HTTP | Endpoint | Query Parameters | 비고 |
|---|---|---|---|---|
| `confluence page get <id>` | GET | `/rest/api/content/{id}` | `expand=body.storage,version,space,children.attachment` | body → cfxmark md 변환 |
| `confluence page search <cql>` | GET | `/rest/api/search` | `cql`, `limit` | link-follow 페이지네이션 |
| `confluence page children <id>` | GET | `/rest/api/content/{id}/child/page` + `/child/folder` | `expand`, `start`, `limit` | 별도로 folder도 조회 |
| `confluence page history <id> <ver>` | GET | `/rest/api/content/{id}?status=historical&version={n}` | `expand=body.storage,version,space` | 특정 버전 |
| `confluence page diff <id>` | GET × 2 | `/rest/api/content/{id}?status=historical&version={from\|to}` | 동일 endpoint 2회 | client-side `difflib.unified_diff` |

### 4.2 Space / Comment / Label / Attachment / User

| atls 명령 | HTTP | Endpoint | Query Parameters | 비고 |
|---|---|---|---|---|
| `confluence space tree <key>` | GET | `/rest/api/space/{key}/content` | `expand=ancestors`, `start`, `limit=200` | link-follow. ancestors→parent_id/depth |
| `confluence comment list <page_id>` | GET | `/rest/api/content/{id}/child/comment` | `expand=body.view.value,version`, `depth=all` | body.view → md 변환 |
| `confluence label list <page_id>` | GET | `/rest/api/content/{id}/label` | — | |
| `confluence attachment list <page_id>` | GET | `/rest/api/content/{id}/child/attachment` | `start`, `limit`, `filename` | filename: client-side 필터 (v1) |
| `confluence user search <query>` | GET | `/rest/api/group/{groupName}/member` | `start`, `limit=200` | Server: group member + **client-side fuzzy match** |

---

## 5. Confluence — Write (12건)

### 5.1 Page CRUD

| atls 명령 | HTTP | Endpoint | Request Body | Response | 비고 |
|---|---|---|---|---|---|
| `confluence page create` | POST | `/rest/api/content` | `{type: "page", title, space: {key}, body: {storage: {value, representation}}, ancestors: [{id}]}` | 생성된 페이지 전체 | |
| `confluence page update <id>` | PUT | `/rest/api/content/{id}` | `{title, body: {storage: {value, representation}}, version: {number: N+1}}` | 업데이트된 페이지 | **version 필수** (optimistic concurrency) |
| `confluence page delete <id>` | DELETE | `/rest/api/content/{id}` | — | 204 No Content | |
| `confluence page move <id>` | POST | `/rest/api/content/{id}/move/{position}/target/{targetId}` | — | 이동된 페이지 | position: append/above/below |
| `confluence page images <id>` | GET | `/rest/api/content/{id}/child/attachment` | — | 이미지 첨부만 필터 (client-side) | matrix에서 write로 분류되나 실제 read |

### 5.2 Comment

| atls 명령 | HTTP | Endpoint | Request Body | Response | 비고 |
|---|---|---|---|---|---|
| `confluence comment add <page_id>` | POST | `/rest/api/content/{id}/child/comment` | `{type: "comment", body: {storage: {value, representation}}}` | 생성된 댓글 | |
| `confluence comment reply <comment_id>` | POST | `/rest/api/content/{commentId}/child/comment` | 동일 | 생성된 reply | 댓글의 child comment |

### 5.3 Label

| atls 명령 | HTTP | Endpoint | Request Body | Response | 비고 |
|---|---|---|---|---|---|
| `confluence label add <page_id>` | POST | `/rest/api/content/{id}/label` | `[{name: "...", prefix: "global"}]` | `{results: [{name, prefix, id}]}` | |

### 5.4 Attachment

| atls 명령 | HTTP | Endpoint | Request Body | Response | 비고 |
|---|---|---|---|---|---|
| `confluence attachment upload <page_id>` | POST | `/rest/api/content/{id}/child/attachment` | multipart: `file` (binary), `comment` | `{results: [{id, title, type, ...}]}` | `X-Atlassian-Token: nocheck` 필수 |
| `confluence attachment upload-batch <page_id>` | POST × N | 동일 (반복) | 동일 | 동일 | 순차 호출 |
| `confluence attachment download <att_id>` | GET | `/rest/api/content/{attId}/download` | — | binary stream | |
| `confluence attachment download-all <page_id>` | GET × N | 동일 (반복) | — | — | attachment list → 개별 download |
| `confluence attachment delete <att_id>` | DELETE | `/rest/api/content/{attId}` | — | 204 No Content | |

---

## 6. 구현 시 참고 사항

### 6.1 fields 파라미터 최적화
Jira REST API의 `fields` 파라미터로 응답 크기를 대폭 줄일 수 있다:
- `fields=key,summary,status,assignee,updated` → 필요한 필드만 요청
- `fields=-description` → 특정 필드 제외
- MCP는 이 최적화를 하지 않고 `DEFAULT_READ_JIRA_FIELDS` 전체를 요청

### 6.2 expand 파라미터 최적화
- **body 미요청 시 expand 생략** (RFE-001 R7): `--fields`에 body가 없으면 `expand`에서 body 제거 → 응답 ~1/10
- Confluence: `expand=body.storage` 생략 시 본문 전체가 빠짐

### 6.3 Optimistic Concurrency
- **Confluence page update**: `version.number` = 현재버전 + 1 필수. 불일치 시 409 Conflict
- **Jira**: 표준 concurrency control 없음 (last-write-wins)

### 6.4 클라이언트 사이드 처리가 필요한 명령
- `jira field search` — 전체 필드 fetch → fuzzy match
- `confluence page diff` — 두 버전 fetch → `difflib.unified_diff`
- `confluence user search` (Server) — group member 순회 → fuzzy match
- `confluence attachment list` (Server v1) — 전체 fetch → filename 필터
- `jira issue images` / `confluence page images` — 첨부 목록 → 이미지만 필터

### 6.5 Error 응답 패턴

| HTTP 상태 | 의미 | atls exit code (§15) |
|---|---|---|
| 400 | 유효성 오류 (필드 누락, 잘못된 값) | 7 (validation) |
| 401 | 인증 실패 | 3 (auth) |
| 403 | 권한 부족 | 4 (permission) |
| 404 | 리소스 없음 | 5 (not-found) |
| 409 | 버전 충돌 (Confluence) | 6 (conflict) |
| 429 | Rate limit | 8 (rate-limit), retry-after 표시 |

### 6.6 Server/DC vs Cloud 차이점 요약

| 항목 | Server/DC | Cloud |
|---|---|---|
| Jira API 버전 | v2 (`/rest/api/2/`) | v3 (`/rest/api/3/`) |
| Jira search | GET `/search` | POST `/search/jql` |
| Jira pagination | offset (`startAt`) | token (`nextPageToken`) |
| User identifier | `username` | `accountId` |
| Jira body format | wiki markup | ADF (Atlassian Document Format) |
| Confluence API | v1 (`/rest/api/content/`) | v2 (`/api/v2/pages/`) |
| Confluence user search | group member API | CQL search API |

> 본 프로젝트는 Server/DC 전용. Cloud 경로는 구현하지 않는다.

---

## 7. 소스 참조

| 참조 레포 | 파일 | 역할 |
|---|---|---|
| `mcp-atlassian` | `src/mcp_atlassian/jira/issues.py` | issue get/search/create/update/delete |
| `mcp-atlassian` | `src/mcp_atlassian/jira/search.py` | JQL search, board issues, sprint issues |
| `mcp-atlassian` | `src/mcp_atlassian/jira/comments.py` | comment add/edit |
| `mcp-atlassian` | `src/mcp_atlassian/jira/transitions.py` | transition |
| `mcp-atlassian` | `src/mcp_atlassian/jira/links.py` | issue link create/delete, remote link |
| `mcp-atlassian` | `src/mcp_atlassian/jira/epics.py` | epic link (via update_issue) |
| `mcp-atlassian` | `src/mcp_atlassian/jira/sprints.py` | sprint CRUD, add-issues |
| `mcp-atlassian` | `src/mcp_atlassian/jira/watchers.py` | watcher add/remove |
| `mcp-atlassian` | `src/mcp_atlassian/jira/worklog.py` | worklog add |
| `mcp-atlassian` | `src/mcp_atlassian/jira/fields.py` | field search, fuzzy match |
| `mcp-atlassian` | `src/mcp_atlassian/jira/projects.py` | project list, issues, versions, components |
| `mcp-atlassian` | `src/mcp_atlassian/jira/boards.py` | board list |
| `mcp-atlassian` | `src/mcp_atlassian/confluence/pages.py` | page CRUD, children, tree, history, diff, move |
| `mcp-atlassian` | `src/mcp_atlassian/confluence/search.py` | search, user search |
| `mcp-atlassian` | `src/mcp_atlassian/confluence/comments.py` | comment add, reply |
| `mcp-atlassian` | `src/mcp_atlassian/confluence/labels.py` | label add |
| `mcp-atlassian` | `src/mcp_atlassian/confluence/attachments.py` | attachment upload/download/delete |
| `atlassian-python-api` | `atlassian/jira.py` | REST endpoint URL 패턴 (6048줄) |
| `atlassian-python-api` | `atlassian/confluence/server/__init__.py` | Confluence Server endpoint |
