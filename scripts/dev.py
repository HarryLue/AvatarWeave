#!/usr/bin/env python3
"""Run, stop, or restart the local demo without leaving duplicate services."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime/dev.json"


def process_stamp(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def tracked():
    if not STATE.exists():
        return []
    try:
        return [
            p
            for p in json.loads(STATE.read_text())
            if process_stamp(p["pid"]) == p["stamp"]
        ]
    except (ValueError, KeyError):
        sys.exit(f"Cannot read {STATE}; inspect the state file before starting.")


def stop_tracked():
    processes = tracked()
    for p in reversed(processes):
        try:
            os.killpg(p["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and any(
        process_stamp(p["pid"]) == p["stamp"] for p in processes
    ):
        time.sleep(0.2)
    for p in processes:
        if process_stamp(p["pid"]) == p["stamp"]:
            try:
                os.killpg(p["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    STATE.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-livekit", action="store_true")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--restart",
        action="store_true",
        help="Stop this project's tracked services, then start with current .env",
    )
    action.add_argument(
        "--stop", action="store_true", help="Stop this project's tracked services"
    )
    action.add_argument("--status", action="store_true", help="Show tracked services")
    parser.add_argument(
        "--kokoro-device",
        choices=["cpu", "gpu"],
        help="Override Kokoro device for this run",
    )
    parser.add_argument(
        "--kokoro-gpu-id", type=int, help="CUDA device index visible to this process"
    )
    args = parser.parse_args()
    if args.kokoro_gpu_id is not None and args.kokoro_gpu_id < 0:
        parser.error("--kokoro-gpu-id must be nonnegative")
    os.chdir(ROOT)
    if args.status:
        processes = tracked()
        for p in processes:
            print(f"{p['name']}: running, pid={p['pid']}")
        if not processes:
            print(
                "No tracked services. Older or external services may still occupy ports."
            )
        return
    if args.stop or args.restart:
        stop_tracked()
        if args.stop:
            print("Stopped this project's tracked services.")
            return
    if tracked():
        sys.exit(
            "Services already running. Use: python3 scripts/dev.py --restart (reload .env), --status, or --stop"
        )
    python = ROOT / "backend/.venv/bin/python"
    if not python.exists():
        sys.exit("Run: cd backend && uv sync")
    env = os.environ.copy()
    env["PATH"] = str(ROOT / ".tools/bin") + os.pathsep + env.get("PATH", "")
    values = json.loads(
        subprocess.check_output(
            [
                str(python),
                "-c",
                'from dotenv import dotenv_values; import json; print(json.dumps({k:v for k,v in dotenv_values("../.env").items() if v is not None}))',
            ],
            cwd=ROOT / "backend",
        )
    )
    env.update(values)
    if args.kokoro_device is not None:
        env["KOKORO_DEVICE"] = args.kokoro_device
    if args.kokoro_gpu_id is not None:
        env["KOKORO_GPU_ID"] = str(args.kokoro_gpu_id)
    config = json.loads(
        subprocess.check_output(
            [str(python), str(ROOT / "scripts/check-config.py")], env=env, cwd=ROOT
        )
    )
    managed_kokoro = "kokoro" in config.get("local_services", [])
    ports = (
        ([8880] if managed_kokoro else [])
        + [5174, 8090, 8091]
        + ([] if args.external_livekit else [17880, 17881])
    )
    for port in ports:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                sys.exit(
                    f"Port {port} is already occupied. No services were started. Use --restart for tracked services; inspect older processes with: ss -ltnp"
                )
    STATE.parent.mkdir(exist_ok=True)
    commands = []
    if not args.external_livekit:
        binary = env.get("LIVEKIT_BINARY") or (
            str(ROOT / ".tools/livekit-server")
            if (ROOT / ".tools/livekit-server").exists()
            else shutil.which("livekit-server", path=env["PATH"])
        )
        binary = binary or ""
        if not Path(binary).exists():
            sys.exit(
                "Install livekit-server into .tools/ or set LIVEKIT_BINARY, or use --external-livekit."
            )
        # Keep local server credentials consistent with the API/worker environment.
        server_config_bytes = subprocess.check_output(
            [
                str(python),
                "-c",
                'import os,yaml; from pathlib import Path; p=Path("infra/livekit.yaml"); c=yaml.safe_load(p.read_text()); c["keys"]={os.environ.get("LIVEKIT_API_KEY","devkey"):os.environ.get("LIVEKIT_API_SECRET","avatar-kit-local-development-secret-2026")}; print(yaml.safe_dump(c))',
            ],
            cwd=ROOT,
            env=env,
        )
        runtime_config = STATE.parent / "livekit.yaml"
        runtime_config.write_bytes(server_config_bytes)
        runtime_config.chmod(0o600)
        commands.append(
            ("livekit", [binary, "--config", str(runtime_config), "--dev"], ROOT, 17880)
        )
    if managed_kokoro:
        kokoro_python = python
        if env.get("KOKORO_DEVICE", "cpu").lower() == "gpu":
            kokoro_python = ROOT / "backend/kokoro-runtime-gpu/.venv/bin/python"
            if not kokoro_python.exists():
                sys.exit(
                    "Kokoro GPU runtime missing. Run: bash scripts/deploy.sh install"
                )
        commands.append(
            (
                "kokoro",
                [
                    str(kokoro_python),
                    "-m",
                    "uvicorn",
                    "avatarweave_agent.kokoro_service:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8880",
                ],
                ROOT / "backend",
                8880,
            )
        )
    commands.extend(
        [
            (
                "api",
                [
                    str(python),
                    "-m",
                    "uvicorn",
                    "avatarweave_agent.api:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8090",
                ],
                ROOT / "backend",
                8090,
            ),
            (
                "agent",
                [str(python), "-m", "avatarweave_agent.worker", "start"],
                ROOT / "backend",
                8091,
            ),
            ("frontend", ["npm", "run", "dev", "--", "--strictPort"], ROOT, 5174),
        ]
    )
    children, records = [], []
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    STATE.parent.mkdir(exist_ok=True)

    def check_children():
        for name, child, _ in children:
            if child.poll() is not None:
                # Show only errors from the current run, never raw configuration or credentials.
                raise RuntimeError(
                    f"{name} exited ({child.returncode}); see logs/{name}.log"
                )

    def stop(*_):
        for _, child, _ in reversed(children):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for _, child, stream in children:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            stream.close()
        if STATE.exists():
            current = json.loads(STATE.read_text())
            if current and records and current[0]["pid"] == records[0]["pid"]:
                STATE.unlink(missing_ok=True)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    try:
        for name, command, cwd, port in commands:
            stream = open(logs / f"{name}.log", "a", buffering=1)
            stream.write(f"\n--- New run {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            log_start = (logs / f"{name}.log").stat().st_size
            child = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            children.append((name, child, stream))
            records.append(
                {"name": name, "pid": child.pid, "stamp": process_stamp(child.pid)}
            )
            STATE.write_text(json.dumps(records))
            deadline = time.monotonic() + (180 if name == "kokoro" else 45)
            while True:
                check_children()
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/"
                        + (
                            "api/health"
                            if name == "api"
                            else "health"
                            if name == "kokoro"
                            else ""
                        ),
                        timeout=1,
                    ) as response:
                        if response.status == 200:
                            if name != "agent":
                                break
                            with open(logs / "agent.log", "rb") as agent_log:
                                agent_log.seek(log_start)
                                if (
                                    b'"message": "registered worker"'
                                    in agent_log.read()
                                ):
                                    break
                except (OSError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"{name} did not become ready; see logs/{name}.log"
                    )
                time.sleep(0.25)
            print(f"{name}: ready, pid={child.pid}, logs/{name}.log", flush=True)
        print(
            f"Mode: {config['mode']} | Profile: {config['profile']} | Demo: http://localhost:5174 | API: http://localhost:8090/docs",
            flush=True,
        )
        print(
            "Stop: python3 scripts/dev.py --stop | Reload .env: python3 scripts/dev.py --restart",
            flush=True,
        )
        while True:
            check_children()
            time.sleep(1)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    finally:
        stop()


if __name__ == "__main__":
    main()
