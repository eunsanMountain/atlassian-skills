# Test Fixtures

Golden files capturing MCP responses. Used by snapshot tests and token benchmarks.

## How they were collected
- Captured from our internal Jira/Confluence Server instances via the `mcp-atlassian` MCP server
- Capture date: 2026-04-13

## Directory layout
```
fixtures/
├── jira/
│   ├── get-all-projects.json          # 251 projects (token benchmark S1)
│   ├── search-proj.json                # 3 results from PROJ search (S2)
│   ├── get-issue-proj3.json            # single PROJ-3 issue (S3)
│   ├── get-transitions-proj3.json      # PROJ-3 transitions
│   ├── search-fields-epic.json        # epic field search
│   └── get-agile-boards-proj.json      # PROJ board list
├── confluence/
│   ├── search-proj.json                # 3 results from PROJ search (S5)
│   ├── get-page-sample.json        # page (md-converted) (S4)
│   ├── get-page-sample-raw.json    # page (storage XHTML)
│   ├── get-page-history-v1.json       # page v1 history
│   └── get-space-tree-sample.json       # TESTSPACE space tree
└── private/                           # .gitignored — internal data
```

## Caution
- These fixtures contain real internal data
- The `private/` directory is listed in .gitignore
- Sanitize before any public distribution
