from __future__ import annotations

import json
from typing import Any


def format_dry_run(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: Any = None,
    fmt: str = "compact",
) -> str:
    """Format a dry-run payload showing what would be sent.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE)
        url: Full URL
        headers: Headers dict (Authorization masked)
        body: Request body (dict or str)
        fmt: Output format (compact or json)
    """
    # Mask auth headers
    safe_headers: dict[str, str] = {}
    if headers:
        for k, v in headers.items():
            if k.lower() == "authorization":
                safe_headers[k] = v.split()[0] + " ***" if " " in v else "***"
            else:
                safe_headers[k] = v

    if fmt == "json":
        payload = {
            "dry_run": True,
            "method": method,
            "url": url,
            "headers": safe_headers,
            "body": body,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # compact format
    lines = [
        f"[DRY RUN] {method} {url}",
    ]
    if safe_headers:
        for k, v in safe_headers.items():
            lines.append(f"  {k}: {v}")
    if body:
        if isinstance(body, dict):
            lines.append(f"  Body: {json.dumps(body, ensure_ascii=False)}")
        else:
            body_str = str(body)
            if len(body_str) > 200:
                lines.append(f"  Body: {body_str[:200]}... ({len(body_str)} chars)")
            else:
                lines.append(f"  Body: {body_str}")
    return "\n".join(lines)
