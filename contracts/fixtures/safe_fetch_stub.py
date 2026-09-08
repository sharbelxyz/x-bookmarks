"""C6 safe-fetch DENY-ALL stub (contract revision c1).

Consumers (lanes 01/04) import this until lane 05's real provider is
integrated. It conforms to the frozen signature and result shape and denies
every fetch with denied_reason="provider_unavailable" — deny by default is
the frozen fallback behavior. Never "improve" this stub into a real fetcher;
that is lane 05's owned implementation.
"""


def safe_fetch(url, *, max_bytes=4_000_000, timeout=20.0, max_redirects=4,
               allowed_content_types=(), dest_dir=None):
    return {
        "ok": False,
        "url": str(url),
        "final_url": None,
        "status": None,
        "content_type": None,
        "bytes": 0,
        "body_path": None,
        "text": None,
        "error": None,
        "denied_reason": "provider_unavailable",
    }
