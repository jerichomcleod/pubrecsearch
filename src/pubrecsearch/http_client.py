"""Central httpx client factory.

All scrapers should use `make_client()` rather than constructing httpx.Client
directly. This ensures:

  1. truststore is used when available — routes TLS through the OS native cert
     store (macOS Security framework, Windows CertStore, Linux NSS). This fixes
     sites like oig.hhs.gov that use Entrust intermediates without sending the
     full chain, which standard certifi bundles cannot verify.

  2. Consistent default timeouts and follow_redirects behaviour.

Usage:
    from ..http_client import make_client

    with make_client(timeout=120, headers=MY_HEADERS) as client:
        resp = client.get(url)
"""

import ssl

import httpx

try:
    import truststore as _truststore

    _SSL_CTX = _truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    _TRUSTSTORE_AVAILABLE = True
except Exception:
    _SSL_CTX = True          # httpx default (certifi)
    _TRUSTSTORE_AVAILABLE = False


def make_client(
    timeout: float = 60,
    follow_redirects: bool = True,
    headers: dict | None = None,
    **kwargs,
) -> httpx.Client:
    """Return an httpx.Client configured to use the OS native cert store.

    All keyword args are forwarded to httpx.Client (e.g. timeout, headers).
    Callers should use this as a context manager:

        with make_client(timeout=120) as client:
            resp = client.get(url)
    """
    return httpx.Client(
        verify=_SSL_CTX,
        timeout=timeout,
        follow_redirects=follow_redirects,
        headers=headers or {},
        **kwargs,
    )
