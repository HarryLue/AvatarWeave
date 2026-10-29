#!/usr/bin/env bash
# Linux deployment/bootstrap. Toolchains are local; credentials are never printed.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$ROOT_DIR/.tools"
TOOLS_BIN="$TOOLS_DIR/bin"
export PATH="$TOOLS_BIN:$PATH"
export UV_CACHE_DIR="$ROOT_DIR/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$TOOLS_DIR/python"
export UV_LINK_MODE=copy
ACTION="${1:-up}"
if [[ $# -gt 0 ]]; then shift; fi
SKIP_SYSTEM=0
EXTRAS=()
for option in "$@"; do
  case "$option" in
    --skip-system-deps) SKIP_SYSTEM=1 ;;
    --all-providers) EXTRAS=(--all-extras) ;;
    *) printf 'Unknown option: %s\n' "$option" >&2; exit 2 ;;
  esac
done
case "$ACTION" in
  up|install|start|restart|stop|status) ;;
  *) printf 'Usage: bash scripts/deploy.sh [up|install|start|restart|stop|status] [--skip-system-deps] [--all-providers]\n'; exit 2 ;;
esac
cd "$ROOT_DIR"
mkdir -p "$TOOLS_BIN" "$ROOT_DIR/.cache/downloads"
if [[ "$ACTION" == install || "$ACTION" == up ]]; then
  [[ "$(uname -s)" == Linux ]] || { echo 'This bootstrap targets Linux (amd64/arm64).'; exit 1; }
  case "$(uname -m)" in
    x86_64) NODE_ARCH=x64; LK_ARCH=amd64 ;;
    aarch64|arm64) NODE_ARCH=arm64; LK_ARCH=arm64 ;;
    *) echo 'Unsupported CPU architecture'; exit 1 ;;
  esac
  if [[ "$SKIP_SYSTEM" == 0 ]]; then
    if command -v apt-get >/dev/null; then
      PRIVILEGE=()
      if [[ "$EUID" != 0 ]]; then
        command -v sudo >/dev/null || { echo 'Need sudo for OS packages, or use --skip-system-deps after installing prerequisites.'; exit 1; }
        PRIVILEGE=(sudo)
      fi
      "${PRIVILEGE[@]}" apt-get update
      "${PRIVILEGE[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl tar xz-utils libstdc++6 libgomp1 libportaudio2 ffmpeg espeak-ng
    else
      echo 'Install curl, tar, xz, CA certificates, libstdc++, libgomp, libportaudio and FFmpeg with your OS package manager, then use --skip-system-deps.' >&2
      exit 1
    fi
  fi
  for tool in curl tar xz sha256sum; do command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool"; exit 1; }; done
  curl_download() { curl --fail --location --retry 3 --connect-timeout 20 --max-time 600 "$1" -o "$2"; }
  if ! command -v node >/dev/null || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)'; then
    echo 'Installing Node.js 22 LTS locally...'
    curl_download https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt "$ROOT_DIR/.cache/downloads/node-checksums.txt"
    NODE_ARCHIVE="$(awk -v suffix="-linux-${NODE_ARCH}.tar.xz" 'index($2,suffix) && substr($2,length($2)-length(suffix)+1)==suffix {print $2; exit}' "$ROOT_DIR/.cache/downloads/node-checksums.txt")"
    [[ -n "$NODE_ARCHIVE" ]] || { echo 'Cannot select Node.js release'; exit 1; }
    curl_download "https://nodejs.org/dist/latest-v22.x/$NODE_ARCHIVE" "$ROOT_DIR/.cache/downloads/$NODE_ARCHIVE"
    (cd "$ROOT_DIR/.cache/downloads"; awk -v file="$NODE_ARCHIVE" '$2==file' node-checksums.txt | sha256sum --check -)
    mkdir -p "$TOOLS_DIR/node"
    tar -xJf "$ROOT_DIR/.cache/downloads/$NODE_ARCHIVE" --strip-components=1 -C "$TOOLS_DIR/node"
    for tool in node npm npx; do ln -sfn "$TOOLS_DIR/node/bin/$tool" "$TOOLS_BIN/$tool"; done
  fi
  if ! command -v uv >/dev/null; then
    echo 'Installing uv locally...'
    curl_download https://astral.sh/uv/0.11.19/install.sh "$ROOT_DIR/.cache/downloads/uv-install.sh"
    UV_UNMANAGED_INSTALL="$TOOLS_BIN" sh "$ROOT_DIR/.cache/downloads/uv-install.sh"
  fi
  LIVEKIT_VERSION=1.13.11
  if [[ ! -x "$TOOLS_DIR/livekit-server" ]] || ! "$TOOLS_DIR/livekit-server" --version | awk -v version="$LIVEKIT_VERSION" '$NF==version {found=1} END{exit !found}'; then
    echo "Installing LiveKit Server $LIVEKIT_VERSION locally..."
    LIVEKIT_ARCHIVE="livekit_${LIVEKIT_VERSION}_linux_${LK_ARCH}.tar.gz"
    curl_download "https://github.com/livekit/livekit/releases/download/v${LIVEKIT_VERSION}/$LIVEKIT_ARCHIVE" "$ROOT_DIR/.cache/downloads/$LIVEKIT_ARCHIVE"
    curl_download "https://github.com/livekit/livekit/releases/download/v${LIVEKIT_VERSION}/checksums.txt" "$ROOT_DIR/.cache/downloads/livekit-checksums.txt"
    (cd "$ROOT_DIR/.cache/downloads"; awk -v file="$LIVEKIT_ARCHIVE" '$2==file' livekit-checksums.txt > livekit-selected-checksum.txt; test -s livekit-selected-checksum.txt; sha256sum --check livekit-selected-checksum.txt)
    tar -xzf "$ROOT_DIR/.cache/downloads/$LIVEKIT_ARCHIVE" -C "$TOOLS_DIR" livekit-server
  fi
  if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    echo 'Created .env (mock mode). Edit credentials and config/pipeline.yaml before real voice use.'
  fi
  # No global Python installation required: uv provisions the project interpreter.
  uv python install 3.11
  uv sync --locked --project backend --python 3.11 "${EXTRAS[@]}"
  if [[ ${#EXTRAS[@]} == 0 ]]; then
    SELECTED_EXTRAS="$(backend/.venv/bin/python -c 'import sys; sys.path.insert(0,"backend"); from avatarweave_agent.pipeline import load_pipeline,EXTRAS; c=load_pipeline(); print(" ".join(sorted({EXTRAS[s.provider] for s in (c.llm,c.stt,c.tts) if s and s.provider in EXTRAS})))')"
    for extra in $SELECTED_EXTRAS; do EXTRAS+=(--extra "$extra"); done
    if [[ ${#EXTRAS[@]} -gt 0 ]]; then uv sync --locked --project backend --python 3.11 "${EXTRAS[@]}"; fi
  fi
  KOKORO_DEVICE="$(backend/.venv/bin/python -c 'import sys; sys.path.insert(0,"backend"); from avatarweave_agent.config import settings; print(settings.kokoro_device)')"
  if [[ "$KOKORO_DEVICE" == gpu ]]; then
    [[ "$(uname -m)" == x86_64 ]] || { echo 'Kokoro CUDA runtime currently supports Linux x86_64.' >&2; exit 1; }
    uv sync --locked --project backend/kokoro-runtime-gpu --python 3.11
  fi
  backend/.venv/bin/python scripts/download-kokoro.py
  export npm_config_cache="$ROOT_DIR/.cache/npm"
  npm ci
  npm run build
  backend/.venv/bin/python scripts/check-config.py
  echo 'Installation complete. Existing .env and database were preserved.'
fi
case "$ACTION" in
  install) exit 0 ;;
  stop) exec backend/.venv/bin/python scripts/dev.py --stop ;;
  status) exec backend/.venv/bin/python scripts/dev.py --status ;;
  start) exec backend/.venv/bin/python scripts/dev.py ;;
  restart|up) exec backend/.venv/bin/python scripts/dev.py --restart ;;
esac
