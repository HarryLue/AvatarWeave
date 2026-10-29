"""Isolated real LiveKit + mock agent + mobile browser acceptance. No paid models."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    # Fail before launching anything rather than accidentally testing an existing dev server.
    for port in (17890, 17891, 8095, 8096, 5176):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    directory = Path(tempfile.mkdtemp(prefix="avatarweave-practice-smoke-"))
    config = directory / "livekit.yaml"
    config.write_text("""port: 17890
rtc:
  tcp_port: 17891
  port_range_start: 50300
  port_range_end: 50320
  use_external_ip: false
keys:
  practice-smoke: practice-smoke-local-secret-at-least-32-characters
""")
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{directory / 'test.db'}",
        "PIPELINE_MODE": "pipeline" if "--pipeline" in sys.argv else "mock",
        "MOCK_TTS_PREVIEW": "false",
        "AGENT_NAME": "practice-smoke",
        "ALLOWED_AGENTS": "practice-smoke",
        "AGENT_HEALTH_PORT": "8096",
        "LIVEKIT_URL": "ws://127.0.0.1:17890",
        "RTC_PUBLIC_URL": "ws://127.0.0.1:17890",
        "LIVEKIT_API_KEY": "practice-smoke",
        "LIVEKIT_API_SECRET": "practice-smoke-local-secret-at-least-32-characters",
        "SPEAKING_API_URL": "http://127.0.0.1:8095",
        "PRACTICE_SMOKE_URL": "http://127.0.0.1:5176/speaking/",
        "PRACTICE_SMOKE_ARTIFACTS": str(directory),
        "PRACTICE_SMOKE_MODE": "pipeline" if "--pipeline" in sys.argv else "mock",
        "AGENT_HTTP_PROXY": "",
    }
    processes, logs = [], []

    def spawn(name, command, cwd):
        log = (directory / f"{name}.log").open("w")
        logs.append(log)
        processes.append(
            subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )

    def wait_url(url):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError(f"Service exited; logs: {directory}")
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(0.3)
        raise TimeoutError(url)

    try:
        python = str(ROOT / "backend/.venv/bin/python")
        spawn(
            "livekit",
            [str(ROOT / ".tools/livekit-server"), "--config", str(config), "--dev"],
            ROOT,
        )
        wait_url("http://127.0.0.1:17890")
        spawn(
            "api",
            [
                python,
                "-m",
                "uvicorn",
                "avatarweave_agent.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8095",
            ],
            ROOT / "backend",
        )
        wait_url("http://127.0.0.1:8095/api/health")
        spawn(
            "agent",
            [python, "-m", "avatarweave_agent.worker", "start"],
            ROOT / "backend",
        )
        wait_url("http://127.0.0.1:8096")
        spawn(
            "frontend",
            [
                "node",
                str(ROOT / "node_modules/vite/bin/vite.js"),
                "preview",
                "--host",
                "127.0.0.1",
                "--port",
                "5176",
                "--strictPort",
            ],
            ROOT / "apps/speaking",
        )
        wait_url(env["PRACTICE_SMOKE_URL"])
        subprocess.run(
            ["node", "scripts/browser-practice-smoke.mjs"],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=210,
        )
        print(json.dumps({"result": "PASS", "artifacts": str(directory)}))
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
