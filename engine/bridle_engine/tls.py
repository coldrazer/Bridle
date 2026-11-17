"""Self-signed TLS certificate for serving HTTPS on the local network.

Clients can pin the SHA-256 fingerprint shown in the app instead of trusting a CA."""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
from pathlib import Path


def ensure_certificate(directory: Path, hostname: str, addresses: list[str]) -> tuple[Path, Path, str]:
    """Returns (cert_path, key_path, sha256 fingerprint), creating or renewing as needed."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / "bridle-cert.pem", directory / "bridle-key.pem"
    names = sorted({hostname, f"{hostname}.local", "localhost"})
    ips = sorted({*addresses, "127.0.0.1"})

    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have_ips = sorted(str(i) for i in san.get_values_for_type(x509.IPAddress))
            have_names = sorted(san.get_values_for_type(x509.DNSName))
        except x509.ExtensionNotFound:
            have_ips, have_names = [], []
        expires = cert.not_valid_after_utc
        if have_ips == ips and have_names == names and expires - dt.datetime.now(dt.timezone.utc) > dt.timedelta(days=30):
            return cert_path, key_path, _fingerprint(cert)

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Bridle on {hostname}"),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Bridle (self-signed)")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    # Create the private key 0600 from the first byte (no window at the default umask).
    key_path.unlink(missing_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path, _fingerprint(cert)


def _fingerprint(cert) -> str:
    from cryptography.hazmat.primitives import hashes

    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
