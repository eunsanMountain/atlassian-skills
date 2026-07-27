from __future__ import annotations

import os
import ssl
from pathlib import Path

from atlassian_skills.core.errors import ValidationError


def build_ssl_context(ca_bundle: str | None) -> ssl.SSLContext | bool:
    """Return the value to hand httpx's ``verify=``.

    httpx 0.28 deprecated ``verify=<str>`` ("Use verify=ssl.create_default_context(...)"),
    so a configured CA bundle is turned into a real ``SSLContext`` here rather
    than passed through as a path. The old form still works today but warns, and
    `httpx>=0.27` has no upper bound in pyproject — the day httpx drops it, only
    corporate users with a `ca_bundle` would break.

    ``True`` is returned when nothing is configured, which preserves httpx's
    ``trust_env`` behaviour: it honours ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` and
    otherwise falls back to certifi.
    """
    if not ca_bundle:
        return True
    path = Path(ca_bundle).expanduser()
    try:
        if path.is_dir():
            # capath is NOT validated at construction time: an unhashed directory
            # is accepted here and only fails later during the handshake. There is
            # nothing to check now — `atls doctor` warns about it instead.
            return ssl.create_default_context(capath=str(path))
        return ssl.create_default_context(cafile=str(path))
    except OSError as error:  # ssl.SSLError is an OSError subclass
        raise ValidationError(
            "CA bundle could not be loaded",
            hint=(
                "ca_bundle must point at a PEM file, or at an OpenSSL hashed directory. "
                "If the file is DER, re-export it as Base-64 X.509 (certutil -encode)."
            ),
            context={"reason": "invalid_ca_bundle", "path": str(path)},
        ) from error


def describe_verify_source(ca_bundle: str | None) -> tuple[str, str | None]:
    """Return ``(description, warning)`` for whatever will verify TLS.

    Used by `atls doctor` so a corporate user can see which trust store is
    actually in play before a handshake fails somewhere less obvious.
    """
    if ca_bundle:
        path = Path(ca_bundle).expanduser()
        if path.is_dir():
            return (
                f"ca_bundle directory ({path})",
                "A directory is used as OpenSSL capath and needs a hashed layout (c_rehash). "
                "A plain folder of .pem files adds no trust and fails only at handshake time.",
            )
        return f"ca_bundle file ({path})", None
    cert_file = os.environ.get("SSL_CERT_FILE")
    if cert_file:
        return f"SSL_CERT_FILE ({cert_file})", None
    cert_dir = os.environ.get("SSL_CERT_DIR")
    if cert_dir:
        return (
            f"SSL_CERT_DIR ({cert_dir})",
            "SSL_CERT_DIR needs an OpenSSL hashed layout (c_rehash), not a plain directory of PEM files. "
            "Prefer SSL_CERT_FILE with a single bundle.",
        )
    return "certifi (bundled default)", None
