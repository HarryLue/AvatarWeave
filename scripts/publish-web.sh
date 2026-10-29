#!/usr/bin/env bash
# Publish web builds through the existing HTTP/HTTPS site, retaining its certificates.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PRIVILEGE=()
if [[ "$EUID" != 0 ]]; then PRIVILEGE=(sudo); fi
NGINX_BIN="$(command -v nginx || true)"
if [[ -z "$NGINX_BIN" && -x /usr/sbin/nginx ]]; then NGINX_BIN=/usr/sbin/nginx; fi
[[ -n "$NGINX_BIN" ]] || { echo 'Install the site first: bash scripts/deploy.sh install' >&2; exit 1; }
"${PRIVILEGE[@]}" test -f /etc/nginx/snippets/avatarweave-routes.conf
"${PRIVILEGE[@]}" "$NGINX_BIN" -t
npm run build
[[ -f apps/demo/dist/speaking/index.html ]]
BACKUP_DIR="$ROOT_DIR/.runtime/web-publish-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
"${PRIVILEGE[@]}" cp /etc/nginx/snippets/avatarweave-routes.conf "$BACKUP_DIR/routes.conf"
"${PRIVILEGE[@]}" install -m 644 infra/nginx-routes.conf /etc/nginx/snippets/avatarweave-routes.conf
if ! "${PRIVILEGE[@]}" "$NGINX_BIN" -t; then
  "${PRIVILEGE[@]}" install -m 644 "$BACKUP_DIR/routes.conf" /etc/nginx/snippets/avatarweave-routes.conf
  echo "Nginx validation failed; previous routes restored. Backup: $BACKUP_DIR" >&2
  exit 1
fi
# Keep existing hashed assets so clients with an older page can still finish loading.
"${PRIVILEGE[@]}" cp -R apps/demo/dist/. /var/www/avatarweave/
"${PRIVILEGE[@]}" chmod -R a+rX /var/www/avatarweave
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
  "${PRIVILEGE[@]}" systemctl reload nginx
else
  "${PRIVILEGE[@]}" service nginx reload
fi
echo 'Published: / (Demo), /speaking/ (mobile practice), existing HTTPS and WSS retained.'
echo 'Backend changes require a separate project service restart.'
