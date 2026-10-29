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
    x86_64) NODE_ARCH=x64; RTC_ARCH=amd64 ;;
    aarch64|arm64) NODE_ARCH=arm64; RTC_ARCH=arm64 ;;
    *) echo 'Unsupported CPU architecture'; exit 1 ;;
  esac
  PRIVILEGE=()
  if [[ "$EUID" != 0 ]]; then
    command -v sudo >/dev/null || { echo 'Need sudo to deploy the Nginx configuration and frontend.' >&2; exit 1; }
    PRIVILEGE=(sudo)
  fi
  NGINX_BIN="$(command -v nginx || true)"
  if [[ -z "$NGINX_BIN" && -x /usr/sbin/nginx ]]; then NGINX_BIN=/usr/sbin/nginx; fi
  if [[ "$SKIP_SYSTEM" == 0 ]]; then
    if command -v apt-get >/dev/null; then
      SYSTEM_PACKAGES=(ca-certificates curl tar xz-utils libstdc++6 libgomp1 libportaudio2 ffmpeg espeak-ng openssl cron)
      if [[ -z "$NGINX_BIN" ]]; then SYSTEM_PACKAGES+=(nginx); fi
      "${PRIVILEGE[@]}" apt-get update
      "${PRIVILEGE[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${SYSTEM_PACKAGES[@]}"
    else
      echo 'Install nginx, curl, tar, xz, CA certificates, libstdc++, libgomp, libportaudio and FFmpeg with your OS package manager, then use --skip-system-deps.' >&2
      exit 1
    fi
  fi
  NGINX_BIN="${NGINX_BIN:-$(command -v nginx || true)}"
  if [[ -z "$NGINX_BIN" && -x /usr/sbin/nginx ]]; then NGINX_BIN=/usr/sbin/nginx; fi
  [[ -n "$NGINX_BIN" ]] || { echo 'Nginx is required. Install it first, or omit --skip-system-deps.' >&2; exit 1; }
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
  if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    echo 'Created .env (mock mode). Edit credentials and config/pipeline.yaml before real voice use.'
  fi
  # No global Python installation required: uv provisions the project interpreter.
  uv python install 3.11
  # Install the pinned signaling runtime from the dependency manifest.
  uv run --no-project --python 3.11 scripts/install-runtime.py --arch "$RTC_ARCH"
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
  CERTBOT_REQUIREMENT="$(backend/.venv/bin/python -c 'import json; print(json.load(open("infra/https.json"))["certbot_requirement"])')"
  if [[ ! -x "$TOOLS_DIR/certbot/bin/python" ]]; then uv venv --python 3.11 "$TOOLS_DIR/certbot"; fi
  uv pip install --index-url https://pypi.org/simple --python "$TOOLS_DIR/certbot/bin/python" "$CERTBOT_REQUIREMENT"
  backend/.venv/bin/python scripts/download-kokoro.py
  export npm_config_cache="$ROOT_DIR/.cache/npm"
  npm ci
  npm run build
  backend/.venv/bin/python scripts/check-config.py
  # Use a fixed web root so nginx workers need no access to the checkout.
  "${PRIVILEGE[@]}" install -d -m 755 /var/www/avatarweave /etc/nginx/conf.d /etc/nginx/snippets
  "${PRIVILEGE[@]}" cp -R apps/demo/dist/. /var/www/avatarweave/
  "${PRIVILEGE[@]}" chmod -R a+rX /var/www/avatarweave
  "${PRIVILEGE[@]}" install -m 644 infra/nginx-routes.conf /etc/nginx/snippets/avatarweave-routes.conf
  "${PRIVILEGE[@]}" install -m 644 infra/nginx.conf /etc/nginx/conf.d/avatarweave.conf
  # Disable Debian/Ubuntu's default site, preserving it outside sites-enabled/.
  if [[ -L /etc/nginx/sites-enabled/default && "$(readlink /etc/nginx/sites-enabled/default)" == /etc/nginx/sites-available/default ]]; then
    "${PRIVILEGE[@]}" mv /etc/nginx/sites-enabled/default /etc/nginx/avatarweave-default-site.backup
  fi
  "${PRIVILEGE[@]}" "$NGINX_BIN" -t
  if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
    "${PRIVILEGE[@]}" systemctl enable nginx
    if "${PRIVILEGE[@]}" systemctl is-active --quiet nginx; then
      "${PRIVILEGE[@]}" systemctl reload nginx
    else
      "${PRIVILEGE[@]}" systemctl start nginx
    fi
  else
    # SysV/container hosts without a running systemd.
    "${PRIVILEGE[@]}" service nginx status >/dev/null 2>&1 && NGINX_RUNNING=1 || NGINX_RUNNING=0
    if [[ "$NGINX_RUNNING" == 1 ]]; then
      "${PRIVILEGE[@]}" service nginx reload
    else
      "${PRIVILEGE[@]}" service nginx start
    fi
  fi
  # HTTP must be serving the ACME challenge before requesting a certificate.
  "${PRIVILEGE[@]}" "$TOOLS_DIR/certbot/bin/python" scripts/configure-https.py
  echo 'Nginx ready: / (frontend), /api/ (API), /docs, /realtime/ (signaling); HTTP and HTTPS enabled; hosts/certificates follow infra/https.json.'
  echo 'Installation complete. Existing .env and database were preserved.'
fi
case "$ACTION" in
  install) exit 0 ;;
  stop) exec backend/.venv/bin/python scripts/dev.py --stop ;;
  status) exec backend/.venv/bin/python scripts/dev.py --status ;;
  start) exec backend/.venv/bin/python scripts/dev.py ;;
  restart|up) exec backend/.venv/bin/python scripts/dev.py --restart ;;
esac
