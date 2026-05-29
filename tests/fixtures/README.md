# Test Fixtures

Synthetic, hand-authored sample data modeled on the Atlassian Server/DC REST API
response shapes. Used by the unit, contract, snapshot, and token-benchmark suites.

These files do **not** contain any real or captured data. The domain content is a
fictional "search / reporting service" software project; identifiers such as
`PROJ`/`DEMO`/`TEST`/`TESTSPACE`, users like `testuser`/`testuser2`, and example
emails (`@example.com`) are placeholders chosen to exercise the parsers and
formatters (including CJK text handling).

## How they are authored
- Each JSON file mirrors the field shape of a specific Atlassian REST endpoint
  (or the `mcp-atlassian` response envelope) so the pydantic models and formatters
  can be validated against a stable, realistic structure.
- Values are invented for testing only and are safe for public distribution.

## Directory layout
```
fixtures/
├── jira/
│   ├── get-all-projects.json          # project list (token benchmark S1)
│   ├── search-proj.json                # 3 results from a PROJ search (S2)
│   ├── get-issue-proj3.json            # single PROJ-3 issue (S3)
│   ├── get-transitions-proj3.json      # PROJ-3 transitions
│   ├── search-fields-epic.json        # epic field search
│   └── get-agile-boards-proj.json      # PROJ board list
├── confluence/
│   ├── search-proj.json                # 3 results from a PROJ search (S5)
│   ├── get-page-sample.json        # page (md-converted) (S4)
│   ├── get-page-sample-raw.json    # page (storage XHTML)
│   ├── get-page-history-v1.json       # page v1 history
│   └── get-space-tree-sample.json       # TESTSPACE space tree
└── private/                           # .gitignored — local-only scratch
```

## Notes
- The `private/` directory is listed in .gitignore and is never committed.
- Keep JSON valid and field shapes stable when editing; the contract and snapshot
  suites assert against them.
