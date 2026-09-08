#!/usr/bin/env python3
"""Safe outbound fetch for untrusted, group-supplied URLs (contract C6, lane 05).

The radar ingests URLs and media references shared by ANY sender in the
group. Fetching them automatically is an SSRF and resource-exhaustion
surface: a hostile URL can point at loopback services (this dashboard, the
Ollama port), RFC1918/link-local ranges, cloud metadata addresses, a host
that re-resolves between check and use, an endless or decompression-bomb
body, or a redirect chain that does any of the above on hop two.

This module is the single fetch provider for lanes 01/04. Frozen public
surface (contracts/CONTRACTS.md C6):

    safe_fetch(url, *, max_bytes=4_000_000, timeout=20.0, max_redirects=4,
               allowed_content_types=(), dest_dir=None) -> dict

Result keys: ok, url, final_url, status, content_type, bytes, body_path,
text, error, denied_reason. ``denied_reason`` uses only the frozen enum:
scheme | private_target | redirect_target | too_large | timeout |
content_type | provider_unavailable | error.

Guarantees, in order of application:

1. Scheme allowlist: https and http only; credentials (userinfo) rejected.
2. Target policy BEFORE any connection: the hostname is resolved and EVERY
   resolved address must be globally routable. Loopback, RFC1918,
   link-local (incl. 169.254.169.254 metadata), CGNAT, unique-local,
   multicast, reserved, unspecified, IPv4-mapped and NAT64-embedded
   non-global targets are denied with ``private_target``.
3. TOCTOU pinning: the connection goes to the exact IP address that passed
   validation (TLS still verifies the certificate against the original
   hostname via SNI), so a DNS answer cannot change between check and use.
4. Redirects are never auto-followed: each hop re-runs the scheme check and
   the full target policy; a violating or excessive hop is
   ``redirect_target``.
5. Budgets: one wall-clock ``timeout`` for the whole call (all hops,
   connect + TLS + headers + body), ``max_bytes`` on the DECODED body, and
   bounded streaming decompression for gzip/deflate replies (the request
   asks for identity; a lying server cannot bomb us).
6. ``allowed_content_types``: empty = any; otherwise the final media type
   must equal an entry, or match a ``"prefix/"`` entry (e.g. ``image/``).
7. Fetched bytes are DATA. They are never executed, never parsed as
   instructions, and file output goes under ``dest_dir`` with a
   content-hash name (0600), never a name derived from the URL.

Consumers that need a HOST allowlist on top (the trusted-image precedent in
group_filter_loop.download_review_images) keep enforcing it at the call
site on ``url`` before the call and on ``final_url`` after; this module
deliberately owns only target *safety*, not per-feature trust policy.

Stdlib only, so the provider can never be "dependency unavailable"; the
deny-all stub in contracts/fixtures/safe_fetch_stub.py remains the fallback
for lanes that have not integrated this module yet.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import socket
import ssl
import time
import urllib.parse
import zlib
from typing import Any, Dict, List, Optional, Tuple

ALLOWED_SCHEMES = ("https", "http")
CHUNK_BYTES = 65536
# NAT64 well-known prefix embeds an IPv4 address that ipaddress classifies
# as global even when the embedded target is loopback/private.
_NAT64_NET = ipaddress.ip_network("64:ff9b::/96")

_TEXTUAL_TYPES = ("application/json", "application/xml", "application/x-ndjson")
_EXTENSION_FOR_TYPE = {
    "text/html": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/xml": ".xml",
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
}


def _result(url: str, **overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "ok": False,
        "url": str(url),
        "final_url": None,
        "status": None,
        "content_type": None,
        "bytes": 0,
        "body_path": None,
        "text": None,
        "error": None,
        "denied_reason": None,
    }
    base.update(overrides)
    return base


def _address_permitted(ip: ipaddress._BaseAddress) -> bool:
    """True only for globally routable unicast addresses.

    Tests monkeypatch THIS function to let controlled loopback fixtures
    through; production code must never do that.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _address_permitted(mapped)
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_NET:
        embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        return _address_permitted(embedded)
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return bool(ip.is_global)


def _resolve_and_check(host: str, port: int) -> Tuple[Optional[List[Tuple[Any, ...]]], Optional[str]]:
    """Resolve host; return (addrinfo_list, None) or (None, denied_reason).

    ALL resolved addresses must pass policy — a mixed answer (one public,
    one loopback) is treated as hostile, because the OS, not us, would pick
    which one a later connect uses. Literal IP hosts are validated directly,
    with no resolver round-trip at all.
    """
    bare = host.strip("[]")
    try:
        literal = ipaddress.ip_address(bare)
    except ValueError:
        literal = None
    if literal is not None:
        if not _address_permitted(literal):
            return None, "policy"
        if literal.version == 6:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (bare, port, 0, 0))], None
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (bare, port))], None
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        return None, "resolve:{}".format(exc)
    if not infos:
        return None, "resolve:empty answer"
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return None, "policy"
        if not _address_permitted(ip):
            return None, "policy"
    return infos, None


def _ssl_context() -> ssl.SSLContext:
    """Default verifying TLS context; tests monkeypatch this to trust a
    fixture CA. check_hostname stays on: the pinned-IP socket still proves
    it reached the host the URL named."""
    return ssl.create_default_context()


def _media_type(content_type: Optional[str]) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _content_type_allowed(media_type: str, allowed: Tuple[str, ...]) -> bool:
    if not allowed:
        return True
    for entry in allowed:
        entry = str(entry).strip().lower()
        if not entry:
            continue
        if entry.endswith("/"):
            if media_type.startswith(entry):
                return True
        elif media_type == entry:
            return True
    return False


def _looks_textual(media_type: str) -> bool:
    return (
        media_type.startswith("text/")
        or media_type in _TEXTUAL_TYPES
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )


def _read_bounded(
    response: http.client.HTTPResponse,
    sock: socket.socket,
    max_bytes: int,
    deadline: float,
) -> Tuple[Optional[bytes], Optional[str], int]:
    """Stream the body within byte and time budgets.

    Returns (body, None, decoded_len) or (None, denied_reason, best_len).
    Handles gzip/deflate transparently with the OUTPUT bounded, so a
    compressed reply cannot expand past max_bytes.
    """
    declared = response.headers.get("Content-Length")
    try:
        if declared is not None and int(declared) > max_bytes:
            return None, "too_large", int(declared)
    except ValueError:
        pass

    encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
    decompressor = None
    if encoding in ("gzip", "x-gzip", "deflate"):
        decompressor = zlib.decompressobj(zlib.MAX_WBITS | 32)
    elif encoding not in ("identity", ""):
        return None, "content_type", 0

    produced = bytearray()
    while True:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            return None, "timeout", len(produced)
        sock.settimeout(remaining_time)
        try:
            chunk = response.read(CHUNK_BYTES)
        except (socket.timeout, ssl.SSLError) as exc:
            if isinstance(exc, ssl.SSLError) and "timed out" not in str(exc).lower():
                return None, "error:{}".format(exc), len(produced)
            return None, "timeout", len(produced)
        except (http.client.HTTPException, OSError) as exc:
            return None, "error:{}".format(exc), len(produced)
        if not chunk:
            break
        if decompressor is not None:
            try:
                budget = max_bytes + 1 - len(produced)
                produced.extend(decompressor.decompress(chunk, budget))
                if decompressor.unconsumed_tail:
                    return None, "too_large", max_bytes + 1
            except zlib.error as exc:
                return None, "error:bad {} stream: {}".format(encoding, exc), len(produced)
        else:
            produced.extend(chunk)
        if len(produced) > max_bytes:
            return None, "too_large", max_bytes + 1
    if decompressor is not None:
        try:
            tail_budget = max_bytes + 1 - len(produced)
            produced.extend(decompressor.flush(max(tail_budget, 1)))
            if len(produced) > max_bytes:
                return None, "too_large", max_bytes + 1
        except zlib.error as exc:
            return None, "error:bad {} stream: {}".format(encoding, exc), len(produced)
    return bytes(produced), None, len(produced)


def _connect_pinned(
    scheme: str,
    host: str,
    port: int,
    infos: List[Tuple[Any, ...]],
    deadline: float,
) -> Tuple[Optional[http.client.HTTPConnection], Optional[socket.socket], Optional[str]]:
    """Connect to a validated address (sequential fallback across the
    answer, every candidate already policy-checked), TLS-wrapped for https
    with SNI/verification against the original hostname."""
    raw = None
    last_error: Optional[str] = None
    for family, _stype, _proto, _canon, sockaddr in infos:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, None, "timeout"
        candidate = socket.socket(family, socket.SOCK_STREAM)
        try:
            candidate.settimeout(remaining)
            candidate.connect(sockaddr)
        except socket.timeout:
            candidate.close()
            return None, None, "timeout"
        except OSError as exc:
            candidate.close()
            last_error = "error:connect failed: {}".format(exc)
            continue
        raw = candidate
        break
    if raw is None:
        return None, None, last_error or "error:connect failed"
    sock: socket.socket = raw
    if scheme == "https":
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raw.close()
                return None, None, "timeout"
            raw.settimeout(remaining)
            sock = _ssl_context().wrap_socket(raw, server_hostname=host)
        except socket.timeout:
            raw.close()
            return None, None, "timeout"
        except (ssl.SSLError, ssl.CertificateError, OSError) as exc:
            raw.close()
            return None, None, "error:tls failed: {}".format(exc)
    # HTTPSConnection only for its default-port/Host-header behavior: with
    # the socket pre-set, connect() never runs, so it does no TLS of its own
    # (the pinned socket above is already wrapped and verified).
    connection_class = (
        http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(
        host if ":" not in host else "[{}]".format(host), port
    )
    connection.sock = sock
    return connection, sock, None


def safe_fetch(
    url: str,
    *,
    max_bytes: int = 4_000_000,
    timeout: float = 20.0,
    max_redirects: int = 4,
    allowed_content_types: tuple = (),
    dest_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch one untrusted URL under the guarantees in the module docstring.

    ``timeout`` is the total wall-clock budget for the whole call, across
    every redirect hop. Textual bodies come back in ``text`` (UTF-8,
    replacement characters for junk); with ``dest_dir`` set, the body is
    written to a content-hash-named file instead and ``body_path`` is set.
    Binary bodies without ``dest_dir`` are counted but not returned.
    """
    original_url = str(url)
    deadline = time.monotonic() + max(float(timeout), 0.001)
    current_url = original_url
    hops = 0

    while True:
        try:
            parts = urllib.parse.urlsplit(current_url)
        except ValueError as exc:
            return _result(original_url, error="unparseable URL: {}".format(exc), denied_reason="error")
        redirect_hop = hops > 0
        scheme = (parts.scheme or "").lower()
        if scheme not in ALLOWED_SCHEMES:
            return _result(
                original_url,
                denied_reason="redirect_target" if redirect_hop else "scheme",
            )
        if parts.username is not None or parts.password is not None:
            return _result(
                original_url,
                error="credentials in URL are not allowed",
                denied_reason="redirect_target" if redirect_hop else "scheme",
            )
        host = parts.hostname
        if not host:
            return _result(original_url, error="URL has no host", denied_reason="error")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError:
            return _result(original_url, error="invalid port", denied_reason="error")

        infos, denial = _resolve_and_check(host, port)
        if denial == "policy":
            return _result(
                original_url,
                denied_reason="redirect_target" if redirect_hop else "private_target",
            )
        if denial is not None:
            return _result(
                original_url,
                error="could not resolve {}: {}".format(host, denial.split(":", 1)[1]),
                denied_reason="error",
            )

        connection, sock, failure = _connect_pinned(scheme, host, port, infos, deadline)
        if failure == "timeout":
            return _result(original_url, error="connect budget exhausted", denied_reason="timeout")
        if failure is not None:
            return _result(original_url, error=failure.split(":", 1)[1], denied_reason="error")

        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            try:
                connection.request(
                    "GET",
                    path,
                    headers={
                        "User-Agent": "group-radar-safe-fetch/1.0",
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                    },
                )
                sock.settimeout(max(deadline - time.monotonic(), 0.001))
                response = connection.getresponse()
            except socket.timeout:
                return _result(original_url, error="response budget exhausted", denied_reason="timeout")
            except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
                return _result(original_url, error="request failed: {}".format(exc), denied_reason="error")

            status = response.status
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    return _result(
                        original_url,
                        final_url=current_url,
                        status=status,
                        error="redirect without Location",
                        denied_reason="error",
                    )
                hops += 1
                if hops > max_redirects:
                    return _result(
                        original_url, status=status, denied_reason="redirect_target"
                    )
                current_url = urllib.parse.urljoin(current_url, location.strip())
                current_url = urllib.parse.urldefrag(current_url)[0]
                continue

            content_type = response.headers.get("Content-Type")
            media = _media_type(content_type)
            if not _content_type_allowed(media, tuple(allowed_content_types)):
                return _result(
                    original_url,
                    final_url=current_url,
                    status=status,
                    content_type=content_type,
                    denied_reason="content_type",
                )

            body, failure, best_len = _read_bounded(response, sock, int(max_bytes), deadline)
            if failure is not None:
                if failure.startswith("error:"):
                    return _result(
                        original_url,
                        final_url=current_url,
                        status=status,
                        content_type=content_type,
                        bytes=best_len,
                        error=failure.split(":", 1)[1],
                        denied_reason="error",
                    )
                reason = failure
                return _result(
                    original_url,
                    final_url=current_url,
                    status=status,
                    content_type=content_type,
                    bytes=best_len,
                    error=(
                        "body exceeded {} bytes".format(max_bytes)
                        if reason == "too_large"
                        else "body read exceeded budget" if reason == "timeout" else None
                    ),
                    denied_reason=reason,
                )
        finally:
            try:
                connection.close()
            except OSError:
                pass

        body_path = None
        text = None
        if dest_dir is not None:
            os.makedirs(dest_dir, exist_ok=True)
            digest = hashlib.sha256(body).hexdigest()[:20]
            extension = _EXTENSION_FOR_TYPE.get(media, ".bin")
            target = os.path.join(dest_dir, digest + extension)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
            body_path = target
        elif _looks_textual(media):
            text = body.decode("utf-8", errors="replace")

        return _result(
            original_url,
            ok=True,
            final_url=current_url,
            status=status,
            content_type=content_type,
            bytes=len(body),
            body_path=body_path,
            text=text,
        )
