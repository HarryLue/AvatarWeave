#!/usr/bin/env python3
"""Pinned Kokoro assets with streaming downloads, SHA256 and atomic cache replacement."""

import hashlib
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from avatarweave_agent.config import settings  # noqa: E402

RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"
)
ASSETS = {
    "kokoro-v1.0.onnx": "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as file:
        for part in iter(lambda: file.read(1024 * 1024), b""):
            value.update(part)
    return value.hexdigest()


def main():
    directory = settings.kokoro_model_dir
    directory.mkdir(parents=True, exist_ok=True)
    model = "kokoro-v1.0.onnx"
    for name in (model, "voices-v1.0.bin"):
        path = directory / name
        if path.exists() and digest(path) == ASSETS[name]:
            print(f"{name}: verified cache", flush=True)
            continue
        temporary = path.with_suffix(path.suffix + ".download")
        print(f"Downloading {name}...", flush=True)
        for attempt in range(3):
            try:
                with (
                    urllib.request.urlopen(RELEASE + name, timeout=120) as response,
                    temporary.open("wb") as file,
                ):
                    while data := response.read(1024 * 1024):
                        file.write(data)
                break
            except (OSError, TimeoutError):
                if attempt == 2:
                    raise
                print(f"Retrying {name} download...", flush=True)
                time.sleep(2 * (attempt + 1))
        if digest(temporary) != ASSETS[name]:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"SHA256 mismatch: {name}")
        os.replace(temporary, path)
        print(f"{name}: SHA256 verified", flush=True)


if __name__ == "__main__":
    main()
