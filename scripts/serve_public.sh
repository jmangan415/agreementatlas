#!/usr/bin/env bash
# Serve the public demo at agreementatlas.com, behind a Cloudflare Tunnel.
#
# This runs a SECOND instance, on its own port, deliberately. The development
# server on :8000 runs in local mode, where /api/families lists every family in
# data/library -- including the licensed OpenText PDFs. Tunnelling to that port
# would publish them. Public-demo mode has no library at all: /api/families
# returns 404 and each visitor gets a private workspace that expires.
#
#   scripts/serve_public.sh          # foreground, ^C to stop
#
# The launchd agent (see DEPLOY.md) runs this same script, so there is one
# definition of what "the public demo" means rather than two that drift.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Operator identity and contact belong to the person deploying, not to the
# repository, so they come from .env (gitignored) when present.
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

export APP_MODE="public-demo"
export HOST="127.0.0.1"          # the tunnel dials out; nothing listens publicly
export PORT="${PUBLIC_PORT:-8001}"
export SESSION_COOKIE_SECURE="true"

# Without this every request arrives from 127.0.0.1 -- the tunnel -- so all
# visitors would share one rate-limit bucket and any single person could lock
# out everyone else. Cloudflare sets CF-Connecting-IP; trust it only because
# nothing but the tunnel can reach this port.
export TRUST_CLOUDFLARE_HEADERS="true"

export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://agreementatlas.com}"
export PUBLIC_OPERATOR_NAME="${PUBLIC_OPERATOR_NAME:-John Mangan}"
export PRIVACY_CONTACT_EMAIL="${PRIVACY_CONTACT_EMAIL:-privacy@agreementatlas.com}"

# A public visitor should not be able to queue work indefinitely.
export SESSION_TTL_HOURS="${SESSION_TTL_HOURS:-6}"
export SESSION_CLEANUP_INTERVAL_SECONDS="${SESSION_CLEANUP_INTERVAL_SECONDS:-30}"
export LMSTUDIO_MAX_CONCURRENT_JOBS="${LMSTUDIO_MAX_CONCURRENT_JOBS:-1}"

exec "$ROOT/.venv/bin/python" app.py
