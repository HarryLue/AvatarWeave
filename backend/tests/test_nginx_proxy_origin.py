"""Exercise real Nginx header inheritance with the deployed route snippet."""

import ipaddress
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_nginx_preserves_https_origin_and_overwrites_untrusted_protocol(tmp_path):
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("Real Nginx is required for proxy integration")

    class Echo(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps(
                {
                    name: self.headers.get(name)
                    for name in ("Host", "X-Forwarded-Proto", "X-Real-IP", "X-AvatarWeave-RTC-URL")
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "proxy-regression")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    root = Path(__file__).resolve().parents[2]
    snippet = tmp_path / "routes.conf"
    snippet.write_text(
        (root / "infra/nginx-routes.conf")
        .read_text()
        .replace("http://127.0.0.1:8090", f"http://127.0.0.1:{upstream.server_port}")
    )
    http_port, https_port = free_port(), free_port()
    config = tmp_path / "nginx.conf"
    config.write_text(f"""
        worker_processes 1;
        pid {tmp_path}/nginx.pid;
        error_log stderr warn;
        events {{ worker_connections 32; }}
        http {{
            access_log off;
            client_body_temp_path {tmp_path}/body;
            proxy_temp_path {tmp_path}/proxy;
            map $http_upgrade $avatarweave_connection_upgrade {{ default upgrade; '' close; }}
            map $scheme $avatarweave_websocket_scheme {{ default ws; https wss; }}
            server {{
                listen 127.0.0.1:{http_port};
                listen 127.0.0.1:{https_port} ssl;
                ssl_certificate {cert_path};
                ssl_certificate_key {key_path};
                include {snippet};
            }}
        }}
    """)
    process = subprocess.Popen(
        [nginx, "-p", str(tmp_path), "-c", str(config), "-g", "daemon off;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            if process.poll() is not None:
                raise AssertionError(
                    "Isolated Nginx could not start: " + process.stderr.read().decode()
                )
            try:
                with socket.create_connection(("127.0.0.1", http_port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("Isolated Nginx startup timed out") from None
                time.sleep(0.02)
        with httpx.Client(
            verify=ssl.create_default_context(cafile=str(cert_path)), trust_env=False
        ) as client:
            for scheme, port in [("http", http_port), ("https", https_port)]:
                result = client.get(
                    f"{scheme}://127.0.0.1:{port}/api/v2/auth/login",
                    headers={"Host": "memory.example.test:8443", "X-Forwarded-Proto": "forged"},
                )
                assert result.status_code == 200
                assert result.json() == {
                    "Host": "memory.example.test:8443",
                    "X-Forwarded-Proto": scheme,
                    "X-Real-IP": "127.0.0.1",
                    "X-AvatarWeave-RTC-URL": ("wss" if scheme == "https" else "ws")
                    + "://memory.example.test:8443/realtime",
                }
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stderr.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
