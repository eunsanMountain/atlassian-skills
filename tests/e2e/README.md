# Live Atlassian E2E tests

These tests can create, update, and delete real Jira issues and Confluence pages. They are disabled unless a live
profile and the target project/space are selected explicitly. Write tests additionally require a one-shot opt-in.

Use a profile already configured by `atls setup`; credentials stay in the configured keyring or command provider:

```bash
export ATLS_E2E_PROFILE=default
export ATLS_E2E_PROJECT=TESTPROJECT
export ATLS_E2E_SPACE=TESTSPACE
export ATLS_E2E_PARENT=12345678       # optional parent for temporary pages
export ATLS_E2E_ALLOW_WRITES=1
uv run pytest tests/e2e -m integration -v
```

Write tests create uniquely named temporary resources first. Jira issues are assigned to the authenticated user, and
Confluence pages are created under `ATLS_E2E_PARENT` when provided. Cleanup failures fail the test instead of being
silently ignored. Never point these variables at a project or space that has not been approved for test writes.
