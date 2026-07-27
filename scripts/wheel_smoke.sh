#!/usr/bin/env bash
# Verify a built wheel in an isolated environment before tagging a release.
#
# Everything the release process claims about the artifact is asserted here with
# an exit code rather than eyeballed: the version the wheel reports, that
# `doctor` runs with no network, and that a failing request produces the new
# diagnostics without leaking the token. No live Atlassian instance is needed —
# a throwaway local server on an ephemeral port plays the part of a proxy that
# answers 302 with no Location.
#
# Usage: bash scripts/wheel_smoke.sh [--skip-build] [version]
#
#   --skip-build   Reuse an existing dist/ instead of running `uv lock --check`
#                  and `uv build`. Used by release.yml, which has already done both.
set -euo pipefail

SKIP_BUILD=0
VERSION=""
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=1 ;;
    -*) echo "FAIL: unknown option $arg"; exit 2 ;;
    *) VERSION="$arg" ;;
  esac
done
VERSION="${VERSION:-$(grep -m1 '^version = ' pyproject.toml | cut -d'"' -f2)}"

if [ "$SKIP_BUILD" -eq 1 ]; then
  echo "Reusing existing dist/ (--skip-build)"
else
  uv lock --check
  uv build >/dev/null
fi

WHL="dist/atlassian_skills-${VERSION}-py3-none-any.whl"
test -f "$WHL" || { echo "FAIL: $WHL not found"; exit 1; }

# The checksum is always printed, but only written to a file when this script
# built dist/ itself. `uv publish` defaults to uploading `dist/*`, so leaving a
# .sha256 beside the wheel during a release would put a non-distribution file
# into the upload set.
if [ "$SKIP_BUILD" -eq 1 ]; then
  sha256sum "$WHL"
else
  sha256sum "$WHL" | tee "dist/atlassian_skills-${VERSION}.whl.sha256"
fi

SMOKE="$(mktemp -d)"
FIXTURE_PID=""
cleanup() {
  [ -n "$FIXTURE_PID" ] && kill "$FIXTURE_PID" 2>/dev/null
  rm -rf "$SMOKE"
  return 0
}
trap cleanup EXIT

# `uv venv` does not install pip (that needs --seed), so `python -m pip` would
# fail here. Install through uv instead.
uv venv "$SMOKE/venv" >/dev/null 2>&1
uv pip install --python "$SMOKE/venv/bin/python" --no-cache "$WHL" >/dev/null 2>&1
ATLS="$SMOKE/venv/bin/atls"
PY="$SMOKE/venv/bin/python"

# 1) The wheel reports the version it claims to be.
"$ATLS" --version | grep -qx "atls ${VERSION}" || { echo "FAIL: --version mismatch"; exit 1; }

# 2) doctor runs offline. --no-update-check keeps the PyPI probe out of it, so
#    the result does not depend on network conditions.
"$ATLS" doctor --no-update-check >/dev/null || { echo "FAIL: doctor"; exit 1; }

# 3) Fixture: 302 with no Location, on an ephemeral port the server picks itself.
"$PY" - "$SMOKE/port" <<'PYEOF' &
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(302)
        self.end_headers()

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[1], "w") as handle:
    handle.write(str(server.server_port))
server.serve_forever()
PYEOF
FIXTURE_PID=$!

for _ in $(seq 100); do [ -s "$SMOKE/port" ] && break; sleep 0.1; done
test -s "$SMOKE/port" || { echo "FAIL: fixture never reported a port"; exit 1; }
PORT="$(cat "$SMOKE/port")"
for _ in $(seq 100); do
  "$PY" -c "import socket;socket.create_connection(('127.0.0.1',$PORT),0.2).close()" 2>/dev/null && break
  sleep 0.1
done

# A profile name nobody has in config.toml. Config wins over the environment, so
# a name like `corp` — the example used throughout our own docs — would send this
# probe to a real corporate Jira with a fake token.
export ATLS_ATLSSMOKE_JIRA_URL="http://127.0.0.1:$PORT"
export ATLS_ATLSSMOKE_JIRA_TOKEN="smoke-token-do-not-log"
PROFILE=atlssmoke

# 4) Verbose path: exit 1, request line present, token absent.
set +e
"$ATLS" --profile "$PROFILE" --verbose 2 jira user me >"$SMOKE/out" 2>"$SMOKE/err"
RC=$?
set -e
[ "$RC" -eq 1 ] || { echo "FAIL: exit=$RC (expected 1)"; cat "$SMOKE/err"; exit 1; }
grep -q "Request: GET .* -> 302" "$SMOKE/err" || { echo "FAIL: no Request line"; exit 1; }
grep -q "^\[atls\] GET " "$SMOKE/err" || { echo "FAIL: no verbose request line"; exit 1; }
grep -q "127.0.0.1:$PORT" "$SMOKE/err" || { echo "FAIL: request did not go to the fixture"; exit 1; }
if grep -q "smoke-token-do-not-log" "$SMOKE/err" "$SMOKE/out"; then
  echo "FAIL: token leaked into output"
  exit 1
fi

# 5) `reason` lives in the JSON envelope; the human renderer prints message and
#    hint only. Redirect it to a file first — under `pipefail`, `atls | grep`
#    returns atls's own exit 1 even when grep matches, so the check would always
#    report failure.
set +e
"$ATLS" --profile "$PROFILE" --format=json jira user me >"$SMOKE/json" 2>/dev/null
set -e
grep -q '"reason": *"redirect_without_location"' "$SMOKE/json" || {
  echo "FAIL: reason not found in JSON envelope"
  cat "$SMOKE/json"
  exit 1
}

echo "wheel smoke PASS (${VERSION}, fixture port ${PORT})"
