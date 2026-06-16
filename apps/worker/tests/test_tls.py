from __future__ import annotations

import os
import ssl
import stat
from pathlib import Path

import httpx
import pytest

from rvc_worker import client as client_module
from rvc_worker import settings as settings_module
from rvc_worker import tls
from rvc_worker.client import HttpManagerClient
from rvc_worker.settings import SettingsError, WorkerSettings

TEST_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIDETCCAfmgAwIBAgIUTygDFDJnWRRXtQOC16reCnbNJuMwDQYJKoZIhvcNAQEL
BQAwGDEWMBQGA1UEAwwNUlZDLVRlc3QtUm9vdDAeFw0yNjA3MTIxMjQwMjBaFw0z
NjA3MDkxMjQwMjBaMBgxFjAUBgNVBAMMDVJWQy1UZXN0LVJvb3QwggEiMA0GCSqG
SIb3DQEBAQUAA4IBDwAwggEKAoIBAQCPknhmYinSzRbC6oWTYJkMkkuIrKChL85j
Er+oUFyeMBcW+73R6SFA3cA4HBnrr3eIE5uiAXLSY36C1AS03jwNd9GDRJ1hHVl4
xSeiLeGnHwlYGfUBVBwKRkx/XDdRiGcQQ+L+O6zhNjrEQ4SvmR/b1m3PRE5i6CA2
gPMO4vZfSB/+2EyCvuFIljU/OxOo1c6/hhqwNc3wmED1n045toy4Ks+tkH/E7RWD
JaY+oLWsSgk0z300SNGFSqZfh6WCfVRk2nt3XBi9KPOl0YIfvk/Nddiy+kFyNQAk
r+CAqcmOpNCqDobUqcHNCz8YCGzkopbPyCbuFi+1c2cm7y9K3CmzAgMBAAGjUzBR
MB0GA1UdDgQWBBRkT7nR3ftVw6NG+YtJeD3DPMb5XDAfBgNVHSMEGDAWgBRkT7nR
3ftVw6NG+YtJeD3DPMb5XDAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUA
A4IBAQCAls0io3M5meaWrdsD0t8y0wHRez31yo7adUhN6MhJEe3c4uh9SW9KsoCc
M5VHDkM6Ruya+ZJRhiSPlO145nuA/gw+n+mjhfA81pZIHgwsOEBIJhXy/mfiwfMY
bpH2v9qlGq+lNrbaqtiVN+44d9qLTNWmTCHqlWSwSTlRyyIUfdLGxG1buFNfxINT
+409lVJsNnTE6KPbuFraNmxhQn67C8qm5+ONhZmq7YyrjlQwO/htV4njafSFtRXj
RhzXRlfwnHCrIb6piI+ZIVgw6DvAs7k4GwJvSCwND9gHBwGGOrLPF4PPTNqdBxJw
C+pd1/8zWYZL28TWNR5KPk8jy0+t
-----END CERTIFICATE-----
"""

HANDSHAKE_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDWDCCAkCgAwIBAgIUI/c3ELE04VJh4zyN2rq9DTkan7wwDQYJKoZIhvcNAQEL
BQAwGjEYMBYGA1UEAwwPbWFuYWdlci5leGFtcGxlMB4XDTI2MDcxMjEyNDcwMFoX
DTM2MDcwOTEyNDcwMFowGjEYMBYGA1UEAwwPbWFuYWdlci5leGFtcGxlMIIBIjAN
BgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAs1udoddNR4V8RBfzYm1XICyhsBU/
QHrUD0CY47/nZrhnwUtmSX/Jc7BoOqr9Em04PXgObfZekvKwjZDX6+HzZzTHOnBc
SWHi8H7il3hYBhWmfysbddZT88aaAV9ddhnNWfKjyt2+wjq7C1dPvb0gGkVdWNNr
OHY70S50hquxzsi4aPI9NyiEYwYUuTNYSa7dLphYSwK6RXpXPzbE46QWj5jpMObA
rOD7UCP2hYTPFm326LMd80uZ2vDYB2U4m1WW7kQZnMba7IdYRP1XzyTXCy99kefU
NZoK1DS4Q+0JAC5kGuMDFPPW0WrgM0t1pMJ/SblkavJJ69OdcgEoQvXF6wIDAQAB
o4GVMIGSMB0GA1UdDgQWBBTvR3NSjrCBseW4wRFZa8/yJb+RmTAfBgNVHSMEGDAW
gBTvR3NSjrCBseW4wRFZa8/yJb+RmTAaBgNVHREEEzARgg9tYW5hZ2VyLmV4YW1w
bGUwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMCAqQwEwYDVR0lBAwwCgYI
KwYBBQUHAwEwDQYJKoZIhvcNAQELBQADggEBAE8K7kXEQX7maDdlY/nXrd1q8A/Q
6LdJ5Zx7R2HQGP6U1jsFL34v98T0jIL/JWkGrZZUmE3IV1Xr7JcFW3A16jpczVeX
i1oMUBpzEfho9DHko+LEqflb/ju7ZTq6uuX1iV42tcHdRWPHUJ5VhhOvJqCVDN8f
gNdgdhX/3P6qdehW5TbW9Z5ob0BDnxf+xEcsyOudGZfuNEVxrcYKCeJbZH+o7Lxk
Qu3fi7fhDxutmSbLQC86RyJS1rFWMW0Dkj6RbNNJrEln1BB/qSn9jG4lZcrI/5/L
hFaTHKzJgArAnlTj5fBaVTmxOxhW78cBZ9Tph6hc3nBZkRbTZmwO4aDgLGw=
-----END CERTIFICATE-----
"""

# Public throwaway key paired only with the static test certificate above. It
# is intentionally embedded so the TLS proof needs neither a network nor an
# external certificate-generation tool; it is never packaged into a release.
HANDSHAKE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCzW52h101HhXxE
F/NibVcgLKGwFT9AetQPQJjjv+dmuGfBS2ZJf8lzsGg6qv0SbTg9eA5t9l6S8rCN
kNfr4fNnNMc6cFxJYeLwfuKXeFgGFaZ/Kxt11lPzxpoBX112Gc1Z8qPK3b7COrsL
V0+9vSAaRV1Y02s4djvRLnSGq7HOyLho8j03KIRjBhS5M1hJrt0umFhLArpFelc/
NsTjpBaPmOkw5sCs4PtQI/aFhM8Wbfbosx3zS5na8NgHZTibVZbuRBmcxtrsh1hE
/VfPJNcLL32R59Q1mgrUNLhD7QkALmQa4wMU89bRauAzS3Wkwn9JuWRq8knr051y
AShC9cXrAgMBAAECggEAA1ukwOHyDXO/VtsH8IDRj8WmJVDQhVmHXN29vw3ZvD+Z
mbHasWeWEvdrjCWlZVHsKqJzyrVBIgkvKwdaM41gbD89t6dg4vLVN+F4RpO3Oi3v
lxPsiLFBkx/VxE7PxiVHhfaYAHVUW/iThevVj1zaHAPOcppKuJzmE82F2vud8Zwi
aMkh8CSV5v4MrPL/KmtJPRc3sEjqX67uQFRWt47H/r7kdxx8f+eczrUT2asuv4h9
4O/gxyxA5Z3b0cbSu+Jzw49C/swxTkHZLPafKASyRDvQkfqY9AG0nzfg28GBMXxH
RInWDWadLC5mb1+duOE07dilxD814zlyRKZy7ikgGQKBgQDcp84UzZX32wTiZTqY
cXC2h1ah6r08+lZcScfW4i8MIMKvL6pBwCyUt7n6o4K5WKETqrjvBi6+4OhLij+V
AM8XqFb9b5hHwQnPbTJH+QHKfauxLuaKvve91vI7iOtrHypOfB/pES3L0b8BlqQ2
zU5rAqmN3vZD4oErQaGu7vfJ9wKBgQDQFlyMSwB+A7zVEREi3IepzwL1R7wRTV8/
Vok1Yb6TMK+VmLfh8lCF0xDHW9iv/CIilDIocqKUEQjgBUESqRddrggeCrehDgpG
iPhPwU3CtY562hpWe0uVnI41CPYm9PJm0aX6QI6iTgAH+DwxmArBpT8SPVUVl/Yf
9w5moc2GrQKBgEzo5GjV267flU1AEXNvHuVQOP8I7driOtXCFbitb86fYJxlVcfD
LwbLHzTgZ0EpRkOvnvQZSJPZmr4xusW52XUahO+jRQXFoCC/o+X4QgqNURnaChtU
Qs/VqurbZ4sX+swOcaTwCvFe81+fXS+I0gt0ixrbGyVAPtuP7cYmnxGzAoGABaEl
PdJloAMf36/Hg+btcKWeEd6v8tPiCMehjKUpT7gYd/aauu0gSo6MPKh6c0Bh+AeE
V93KEjFnyQ+7U/LpI+qqY42srBnCI2hfN2EmZmcdBKvT3JUXVWPX4Q1uA13LFQcE
84E9mpx7mbuYfUr8t4Jt82NfYucqjHDVnecwtT0CgYEAxRW0ZFzYmtGKy96eLdN7
IAd6U8u7puJp1nIJDj9amJ62UKNDM59eE5EeD3APn0llaguVEJS1zRiiJfHV9+8Q
7toxh3RGF8GPhV652A2rKNrw4dk826Zo45H8N2F1ubNIF7fjm4FxkTXzX4ZOj85n
i42i/sX2zUJDMauiyDsgqx4=
-----END PRIVATE KEY-----
"""


def _write_ca(path: Path, content: str = TEST_CA_PEM, mode: int = 0o644) -> None:
    path.write_text(content, encoding="ascii")
    path.chmod(mode)


def _memory_tls_handshake(
    client_context: ssl.SSLContext,
    server_context: ssl.SSLContext,
    *,
    server_hostname: str,
) -> None:
    client_in = ssl.MemoryBIO()
    client_out = ssl.MemoryBIO()
    server_in = ssl.MemoryBIO()
    server_out = ssl.MemoryBIO()
    client = client_context.wrap_bio(
        client_in,
        client_out,
        server_side=False,
        server_hostname=server_hostname,
    )
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    client_done = False
    server_done = False
    for _ in range(20):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        client_payload = client_out.read()
        if client_payload:
            server_in.write(client_payload)
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        server_payload = server_out.read()
        if server_payload:
            client_in.write(server_payload)
        if client_done and server_done:
            return
    raise AssertionError("in-memory TLS handshake did not complete")


def test_custom_ca_reader_enforces_pem_mode_size_and_no_follow(tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    _write_ca(ca)
    assert tls.read_custom_ca_bundle(ca, required_uid=os.getuid()) == TEST_CA_PEM

    ca.chmod(0o600)
    with pytest.raises(tls.CustomCABundleError, match="mode"):
        tls.read_custom_ca_bundle(ca, required_uid=os.getuid())
    ca.chmod(0o644)

    link = tmp_path / "link.pem"
    link.symlink_to(ca)
    with pytest.raises(tls.CustomCABundleError, match="regular non-symlink"):
        tls.read_custom_ca_bundle(link, required_uid=os.getuid())

    _write_ca(ca, TEST_CA_PEM + "-----BEGIN PRIVATE KEY-----\nsecret\n")
    with pytest.raises(tls.CustomCABundleError, match="private key"):
        tls.read_custom_ca_bundle(ca, required_uid=os.getuid())

    _write_ca(ca, "-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----\n")
    with pytest.raises(tls.CustomCABundleError, match="invalid certificate"):
        tls.read_custom_ca_bundle(ca, required_uid=os.getuid())

    ca.write_bytes(b"A" * (tls.MAX_CUSTOM_CA_BUNDLE_BYTES + 1))
    ca.chmod(0o644)
    with pytest.raises(tls.CustomCABundleError, match="between 1"):
        tls.read_custom_ca_bundle(ca, required_uid=os.getuid())


def test_custom_ca_atomic_install_preserves_exact_bytes_and_read_only_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pem"
    destination_root = tmp_path / "config"
    destination_root.mkdir(mode=0o755)
    destination = destination_root / "custom-ca.pem"
    _write_ca(source)

    tls.install_custom_ca_bundle(
        source,
        destination,
        required_source_uid=os.getuid(),
        output_uid=os.getuid(),
        output_gid=os.getgid(),
    )

    assert destination.read_text(encoding="ascii") == TEST_CA_PEM
    assert stat.S_IMODE(destination.stat().st_mode) == 0o444
    assert not destination.is_symlink()
    assert not list(destination_root.glob(".custom-ca.pem.installing.*"))

    target = tmp_path / "redirected.pem"
    _write_ca(target)
    destination.unlink()
    destination.symlink_to(target)
    with pytest.raises(tls.CustomCABundleError, match="regular non-symlink"):
        tls.install_custom_ca_bundle(
            source,
            destination,
            required_source_uid=os.getuid(),
            output_uid=os.getuid(),
            output_gid=os.getgid(),
        )


def test_settings_accept_only_validated_fixed_container_ca_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca = tmp_path / "custom-ca.pem"
    _write_ca(ca)
    original_reader = tls.read_custom_ca_bundle

    def read_as_current_user(
        path: Path,
        *,
        required_uid: int,
        expected_path: Path | None = None,
    ) -> str:
        assert required_uid == 0
        return original_reader(
            path,
            required_uid=os.getuid(),
            expected_path=expected_path,
        )

    monkeypatch.setattr(settings_module, "DEFAULT_CUSTOM_CA_BUNDLE_PATH", ca)
    monkeypatch.setattr(settings_module, "read_custom_ca_bundle", read_as_current_user)
    base = {
        "MANAGER_URL": "https://manager.example",
        "WORKER_NAME": "gpu-01",
        "WORKER_TOKEN": "secret",
        "DATA_ROOT": str(tmp_path / "data"),
    }
    settings = WorkerSettings.from_sources(environ={**base, "WORKER_CA_BUNDLE_PATH": str(ca)})
    assert settings.ca_bundle_path == ca
    assert settings.redacted()["ca_bundle_path"] == str(ca)

    with pytest.raises(SettingsError, match="fixed container path"):
        WorkerSettings.from_sources(
            environ={**base, "WORKER_CA_BUNDLE_PATH": str(tmp_path / "other.pem")}
        )


def test_worker_ssl_context_is_strict_and_shared_by_default_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[object, bool]] = []

    def make_transport(*, verify: object, trust_env: bool) -> httpx.AsyncBaseTransport:
        observed.append((verify, trust_env))
        return httpx.MockTransport(lambda _request: httpx.Response(204))

    monkeypatch.setattr(client_module.httpx, "AsyncHTTPTransport", make_transport)
    client = HttpManagerClient("https://manager.example", "bootstrap")
    manager_transport = client._manager_transport_factory()  # noqa: SLF001
    object_transport = client._object_transport_factory()  # noqa: SLF001

    assert manager_transport is not object_transport
    assert observed == [
        (client._ssl_context, False),  # noqa: SLF001
        (client._ssl_context, False),  # noqa: SLF001
    ]
    assert client._ssl_context.verify_mode == ssl.CERT_REQUIRED  # noqa: SLF001
    assert client._ssl_context.check_hostname is True  # noqa: SLF001
    assert client._ssl_context.minimum_version >= ssl.TLSVersion.TLSv1_2  # noqa: SLF001


def test_custom_ca_enables_verified_tls_but_not_missing_ca_or_hostname_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca = tmp_path / "custom-ca.pem"
    certificate = tmp_path / "server-cert.pem"
    private_key = tmp_path / "server-key.pem"
    _write_ca(ca, HANDSHAKE_CERT_PEM)
    certificate.write_text(HANDSHAKE_CERT_PEM, encoding="ascii")
    private_key.write_text(HANDSHAKE_KEY_PEM, encoding="ascii")
    private_key.chmod(0o600)

    original_reader = tls.read_custom_ca_bundle

    def read_as_current_user(
        path: Path,
        *,
        required_uid: int,
        expected_path: Path | None = None,
    ) -> str:
        assert required_uid == 0
        return original_reader(
            path,
            required_uid=os.getuid(),
            expected_path=expected_path,
        )

    monkeypatch.setattr(tls, "DEFAULT_CUSTOM_CA_BUNDLE_PATH", ca)
    monkeypatch.setattr(tls, "read_custom_ca_bundle", read_as_current_user)
    trusted_context = tls.create_worker_ssl_context(ca)
    default_context = tls.create_worker_ssl_context(None)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(certificate, private_key)

    _memory_tls_handshake(
        trusted_context,
        server_context,
        server_hostname="manager.example",
    )
    with pytest.raises(ssl.SSLCertVerificationError):
        _memory_tls_handshake(
            default_context,
            server_context,
            server_hostname="manager.example",
        )
    with pytest.raises(ssl.SSLCertVerificationError):
        _memory_tls_handshake(
            trusted_context,
            server_context,
            server_hostname="objects.example",
        )


def test_sync_manager_requests_disable_environment_proxy_and_use_shared_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: tuple[object, ...] = ()

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b""

    class Opener:
        def open(self, _request: object, *, timeout: float) -> Response:
            assert timeout == 30
            return Response()

    def capture_handlers(*values: object) -> Opener:
        nonlocal handlers
        handlers = values
        return Opener()

    monkeypatch.setattr(client_module, "build_opener", capture_handlers)
    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
    )
    assert client._request("GET", "/api/v1/workers/me", None, False, None) == (  # noqa: SLF001
        204,
        b"",
    )

    proxy = next(item for item in handlers if isinstance(item, client_module.ProxyHandler))
    https = next(item for item in handlers if isinstance(item, client_module.HTTPSHandler))
    assert proxy.proxies == {}
    assert https._context is client._ssl_context  # type: ignore[attr-defined]  # noqa: SLF001
