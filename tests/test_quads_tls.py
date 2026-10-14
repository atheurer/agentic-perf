from __future__ import annotations

import asyncio
import json
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from providers import quads as quads_module
from providers.quads import QuadsClient


def _base_config(**overrides: str) -> dict[str, str]:
    return {
        "api_host": "quads.example.test",
        "email": "quads@example.test",
        "password": "test-password",
        "owner": "test-owner",
        "ssh_key_path": "/tmp/test-key",
        "default_root_password": "test-root-password",
        **overrides,
    }


class _CapturingHTTPClient:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    async def aclose(self) -> None:
        pass


@dataclass(frozen=True)
class _TestCertificates:
    ca: Path
    untrusted_ca: Path
    server: Path
    wrong_host: Path
    server_key: Path


def _test_certificates(tmp_path: Path) -> _TestCertificates:
    not_before = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    not_after = not_before + timedelta(days=365)

    def make_name(common_name: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])

    def make_ca(common_name: str) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = make_name(common_name)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
                critical=False,
            )
            .sign(private_key, hashes.SHA256())
        )
        return certificate, private_key

    def write_certificate(certificate: x509.Certificate, filename: str) -> Path:
        path = tmp_path / filename
        path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        return path

    ca, ca_key = make_ca("Agentic Perf QUADS TLS Test CA")
    untrusted_ca, _ = make_ca("Untrusted QUADS TLS Test CA")
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def make_server_certificate(
        sans: list[x509.GeneralName], serial: int
    ) -> x509.Certificate:
        return (
            x509.CertificateBuilder()
            .subject_name(make_name("QUADS TLS test server"))
            .issuer_name(ca.subject)
            .public_key(server_key.public_key())
            .serial_number(serial)
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

    server_key_path = tmp_path / "server.key"
    server_key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return _TestCertificates(
        ca=write_certificate(ca, "ca.crt"),
        untrusted_ca=write_certificate(untrusted_ca, "untrusted-ca.crt"),
        server=write_certificate(
            make_server_certificate([x509.IPAddress(IPv4Address("127.0.0.1"))], 1001),
            "server.crt",
        ),
        wrong_host=write_certificate(
            make_server_certificate([x509.DNSName("wrong.example")], 1002),
            "wrong-host.crt",
        ),
        server_key=server_key_path,
    )


def test_https_defaults_to_system_ca_store(monkeypatch: pytest.MonkeyPatch) -> None:
    system_context = ssl.create_default_context()
    create_context = Mock(return_value=system_context)
    captured: dict[str, object] = {}

    def capture_client(**kwargs: object) -> _CapturingHTTPClient:
        client = _CapturingHTTPClient(**kwargs)
        captured.update(client.kwargs)
        return client

    monkeypatch.setattr(quads_module.ssl, "create_default_context", create_context)
    monkeypatch.setattr(quads_module, "AuditedAsyncHTTPClient", capture_client)

    client = QuadsClient(**_base_config())

    create_context.assert_called_once_with(cafile=None)
    assert captured["verify"] is system_context
    assert system_context.verify_mode == ssl.CERT_REQUIRED
    assert system_context.check_hostname is True
    asyncio.run(client.close())


@pytest.mark.asyncio
async def test_from_secrets_loads_configured_ca_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def capture_client(**kwargs: object) -> _CapturingHTTPClient:
        client = _CapturingHTTPClient(**kwargs)
        captured.update(client.kwargs)
        return client

    monkeypatch.setattr(quads_module, "AuditedAsyncHTTPClient", capture_client)
    certificates = _test_certificates(tmp_path)

    class Secrets:
        async def get_secret(self, _name: str) -> str:
            return json.dumps(_base_config(ca_bundle=str(certificates.ca)))

    client = await QuadsClient.from_secrets(Secrets())

    context = captured["verify"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert any(
        (
            attribute[0][0] == "commonName"
            and attribute[0][1] == "Agentic Perf QUADS TLS Test CA"
        )
        for certificate in context.get_ca_certs()
        for attribute in certificate["subject"]
    )
    await client.close()


async def _https_server(
    certificate: Path, key: Path
) -> tuple[asyncio.AbstractServer, int]:
    async def respond(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _request_from_quads(
    server: asyncio.AbstractServer, port: int, ca_bundle: Path
) -> httpx.Response:
    client = QuadsClient(
        api_host=f"127.0.0.1:{port}",
        email="quads@example.test",
        password="test-password",
        owner="test-owner",
        ssh_key_path="/tmp/test-key",
        default_root_password="test-root-password",
        ca_bundle=str(ca_bundle),
    )
    try:
        return await client._client.get(f"{client._base_url}/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_https_accepts_configured_ca_and_rejects_untrusted_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    certificates = _test_certificates(tmp_path)
    server, port = await _https_server(certificates.server, certificates.server_key)
    response = await _request_from_quads(server, port, certificates.ca)
    assert response.status_code == 200
    assert response.text == "ok"

    server, port = await _https_server(certificates.server, certificates.server_key)
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await _request_from_quads(server, port, certificates.untrusted_ca)


@pytest.mark.asyncio
async def test_https_rejects_hostname_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    certificates = _test_certificates(tmp_path)
    server, port = await _https_server(certificates.wrong_host, certificates.server_key)
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await _request_from_quads(server, port, certificates.ca)
