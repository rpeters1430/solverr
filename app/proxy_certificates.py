"""Persistent root CA and short-lived per-host certificates for the opt-in MITM proxy."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import ssl
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class ProxyCertificateAuthority:
    """Owns the persistent CA identity and issues host certificates on demand."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.certificate_path = self.directory / "ca.crt"
        self.private_key_path = self.directory / "ca.key"
        self._certificate: x509.Certificate | None = None
        self._private_key: rsa.RSAPrivateKey | None = None
        self._server_contexts: dict[str, ssl.SSLContext] = {}

    def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        cert_exists = self.certificate_path.exists()
        key_exists = self.private_key_path.exists()
        if cert_exists != key_exists:
            raise RuntimeError("Incomplete forward-proxy CA: ca.crt and ca.key must both exist or both be absent")
        if cert_exists:
            self._load_existing()
            return

        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Solverr Forward Proxy CA")])
        now = datetime.now(timezone.utc)
        self._certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(self._private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self._private_key.public_key()), critical=False)
            .sign(self._private_key, hashes.SHA256())
        )
        self.private_key_path.write_bytes(self._private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        self.certificate_path.write_bytes(self._certificate.public_bytes(serialization.Encoding.PEM))
        os.chmod(self.private_key_path, 0o600)
        os.chmod(self.certificate_path, 0o644)

    def _load_existing(self) -> None:
        try:
            cert = x509.load_pem_x509_certificate(self.certificate_path.read_bytes())
            key = serialization.load_pem_private_key(self.private_key_path.read_bytes(), password=None)
        except Exception as exc:
            raise RuntimeError("Unable to load forward-proxy CA files") from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise RuntimeError("Forward-proxy CA private key must be RSA")
        if key.public_key().public_numbers() != cert.public_key().public_numbers():
            raise RuntimeError("Forward-proxy CA certificate and private key do not match")
        try:
            constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        except x509.ExtensionNotFound as exc:
            raise RuntimeError("Forward-proxy CA certificate is missing CA constraints") from exc
        if not constraints.ca:
            raise RuntimeError("Forward-proxy certificate is not a CA certificate")
        now = datetime.now(timezone.utc)
        if cert.not_valid_before_utc > now or cert.not_valid_after_utc <= now:
            raise RuntimeError("Forward-proxy CA certificate is outside its validity period")
        self._certificate = cert
        self._private_key = key
        os.chmod(self.private_key_path, 0o600)
        os.chmod(self.certificate_path, 0o644)

    def issue_server_context(self, hostname: str) -> ssl.SSLContext:
        if self._certificate is None or self._private_key is None:
            self.initialize()
        cache_key = hostname.lower()
        cached = self._server_contexts.get(cache_key)
        if cached is not None:
            self._server_contexts.pop(cache_key)
            self._server_contexts[cache_key] = cached
            return cached
        cert_pem, key_pem = self._issue_leaf(hostname)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # SSLContext only accepts paths. Keep the leaf private key in a mode-0700
        # temporary directory, load it into OpenSSL, then remove it immediately.
        with tempfile.TemporaryDirectory(prefix="solverr-proxy-cert-") as temp_dir:
            cert_path = Path(temp_dir) / "host.crt"
            key_path = Path(temp_dir) / "host.key"
            cert_path.write_bytes(cert_pem)
            key_path.write_bytes(key_pem)
            os.chmod(cert_path, 0o600)
            os.chmod(key_path, 0o600)
            context.load_cert_chain(str(cert_path), str(key_path))
        self._server_contexts[cache_key] = context
        if len(self._server_contexts) > 256:
            self._server_contexts.pop(next(iter(self._server_contexts)))
        return context

    def _issue_leaf(self, hostname: str) -> tuple[bytes, bytes]:
        assert self._certificate is not None and self._private_key is not None
        try:
            address = ipaddress.ip_address(hostname)
            general_name: x509.GeneralName = x509.IPAddress(address)
            common_name = hostname
        except ValueError:
            common_name = hostname.encode("idna").decode("ascii")
            general_name = x509.DNSName(common_name)

        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64])])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._certificate.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([general_name]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._private_key.public_key()), critical=False)
            .sign(self._private_key, hashes.SHA256())
        )
        key_pem = leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
        return cert_pem, key_pem

    def public_certificate(self) -> bytes:
        if self._certificate is None:
            self.initialize()
        assert self._certificate is not None
        return self._certificate.public_bytes(serialization.Encoding.PEM)

    def public_fingerprint(self) -> str:
        """SHA-256 of the PEM file, matching `sha256sum ca.crt` for enrollment checks."""
        return hashlib.sha256(self.public_certificate()).hexdigest()
