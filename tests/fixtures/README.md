# Test Fixtures

MCP 응답을 캡처한 골든 파일. snapshot 테스트와 토큰 벤치마크에 사용.

## 수집 방법
- `mcp-atlassian` MCP 서버를 통해 사내 Jira/Confluence Server 인스턴스에서 캡처
- 캡처 일자: 2026-04-13

## 디렉토리 구조
```
fixtures/
├── jira/
│   ├── get-all-projects.json          # 251개 프로젝트 (토큰 벤치마크 S1)
│   ├── search-rlm.json                # RLM 프로젝트 검색 3건 (S2)
│   ├── get-issue-rlm3.json            # RLM-3 단건 조회 (S3)
│   ├── get-transitions-rlm3.json      # RLM-3 전이
│   ├── search-fields-epic.json        # epic 필드 검색
│   └── get-agile-boards-rlm.json      # RLM 보드 목록
├── confluence/
│   ├── search-rlm.json                # RLM 검색 3건 (S5)
│   ├── get-page-429140627.json        # 페이지 (md 변환) (S4)
│   ├── get-page-429140627-raw.json    # 페이지 (storage XHTML)
│   ├── get-page-history-v1.json       # 페이지 v1
│   └── get-space-tree-ivsl.json       # IVSL 스페이스 트리
└── private/                           # .gitignore — 사내 데이터
```

## 주의
- 이 픽스처는 실제 사내 데이터를 포함합니다
- `private/` 디렉토리는 .gitignore에 추가되어 있습니다
- 공개 배포 시 sanitize 필요
