from __future__ import annotations

import os
import ssl
from pathlib import Path

from atlassian_skills.core.errors import ValidationError


def _system_trust_context() -> ssl.SSLContext | bool:
    """OS trust store via truststore, or ``True`` (certifi) when unavailable."""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        # truststore is a base dependency, but an unsupported platform or a
        # broken system store must degrade to certifi, not take the CLI down.
        return True


def build_ssl_context(ca_bundle: str | None) -> ssl.SSLContext | bool:
    """Return the value to hand httpx's ``verify=``.

    Precedence: ``ca_bundle`` (explicit, atls-only) → ``SSL_CERT_FILE`` /
    ``SSL_CERT_DIR`` (process-wide env, honoured through httpx's ``trust_env``)
    → the OS trust store via ``truststore``.

    The truststore default is what corporate networks actually need: a
    TLS-inspecting proxy's root CA is already in the OS store (that is why the
    browser works), so most users need no configuration at all — the same
    reasoning that made pip adopt truststore by default (GitHub #16).

    httpx 0.28 deprecated ``verify=<str>``, so a configured CA bundle is turned
    into a real ``SSLContext`` here rather than passed through as a path.
    """
    if not ca_bundle:
        if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
            # True keeps httpx's trust_env behaviour, which reads these vars.
            # `atls doctor` checks that the file actually loads (a broken one
            # would otherwise fail on every request).
            return True
        return _system_trust_context()
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


def _cert_file_warning(cert_file: str) -> str | None:
    """Check that an ``SSL_CERT_FILE`` actually loads, before a request has to fail on it."""
    path = Path(cert_file)
    if not path.is_file():
        return (
            "The file does not exist — every TLS request will fail, in atls and in every "
            "other tool that reads SSL_CERT_FILE (uv, pip). Unset SSL_CERT_FILE to fall "
            "back to the OS trust store (the default since 0.3.2)."
        )
    try:
        ssl.create_default_context(cafile=str(path))
    except OSError as error:
        return (
            f"The file could not be loaded as PEM ({type(error).__name__}: {str(error)[:200]}). "
            "atls and every other tool that reads SSL_CERT_FILE (uv, pip) will reject it. "
            "If it is DER, convert with `certutil -encode`; if it was written by PowerShell "
            "redirection it may be UTF-16 — re-save as ASCII/UTF-8. Or unset SSL_CERT_FILE "
            "to fall back to the OS trust store (the default since 0.3.2)."
        )
    return None


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
        return f"SSL_CERT_FILE ({cert_file})", _cert_file_warning(cert_file)
    cert_dir = os.environ.get("SSL_CERT_DIR")
    if cert_dir:
        return (
            f"SSL_CERT_DIR ({cert_dir})",
            "SSL_CERT_DIR needs an OpenSSL hashed layout (c_rehash), not a plain directory of PEM files. "
            "Prefer SSL_CERT_FILE with a single bundle.",
        )
    try:
        import truststore  # noqa: F401
    except ImportError:
        return "certifi (bundled default)", None
    return "system trust store (OS certificates, via truststore)", None
