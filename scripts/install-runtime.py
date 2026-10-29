#!/usr/bin/env python3
"""Install the pinned signaling runtime using upstream dependency metadata."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def download(url, destination):
    subprocess.run([
        "curl", "--fail", "--location", "--retry", "3",
        "--connect-timeout", "20", "--max-time", "600", url, "-o", str(destination),
    ], check=True)


def install(root, architecture, manifest_path):
    metadata = json.loads(manifest_path.read_text())
    version = metadata["version"]
    target = root / ".tools/rtc-server"
    if target.is_file():
        result = subprocess.run([str(target), "--version"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.split()[-1:] == [version]:
            print(f"Signaling runtime {version} already installed.")
            return
    print(f"Installing signaling runtime {version} locally...", flush=True)
    cache = root / ".cache/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    archive_name = metadata["archive"].format(version=version, arch=architecture)
    release_url = metadata["release_url"].format(version=version)
    archive_path = cache / archive_name
    checksums_path = cache / "rtc-checksums.txt"
    download(f"{release_url}/{archive_name}", archive_path)
    download(f"{release_url}/checksums.txt", checksums_path)
    expected = None
    for line in checksums_path.read_text().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == archive_name:
            expected = fields[0]
            break
    with archive_path.open("rb") as archive_file:
        digest = hashlib.sha256()
        for chunk in iter(lambda: archive_file.read(1024 * 1024), b""):
            digest.update(chunk)
    if expected is None or digest.hexdigest() != expected:
        raise RuntimeError("Signaling runtime checksum verification failed")
    staging = target.with_suffix(".pending")
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            member = archive.getmember(metadata["executable"])
            if not member.isfile():
                raise RuntimeError("Signaling runtime archive must contain a regular executable")
            with archive.extractfile(member) as source, staging.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        staging.chmod(0o755)
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", required=True, choices=["amd64", "arm64"])
    args = parser.parse_args()
    install(ROOT, args.arch, ROOT / "infra/rtc-runtime.json")
