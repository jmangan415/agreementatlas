#!/usr/bin/env bash
# Create the Cloudflare Tunnel and point agreementatlas.com at the local demo.
#
# Run ONCE, after `cloudflared tunnel login`. Safe to re-run: it reuses an
# existing tunnel of the same name rather than creating duplicates.
#
#   cloudflared tunnel login       # browser -- do this first
#   scripts/setup_tunnel.sh

set -euo pipefail

TUNNEL_NAME="${TUNNEL_NAME:-agreementatlas}"
DOMAIN="${DOMAIN:-agreementatlas.com}"
PORT="${PUBLIC_PORT:-8001}"
CF_DIR="$HOME/.cloudflared"

if [[ ! -f "$CF_DIR/cert.pem" ]]; then
  cat >&2 <<'MSG'
Not authenticated with Cloudflare yet.

Run this first (it opens a browser; pick the agreementatlas.com zone):

    cloudflared tunnel login

Then run this script again.
MSG
  exit 1
fi

# `tunnel create` fails if the name is taken, which on a re-run is the normal
# case rather than an error.
if /usr/local/bin/cloudflared tunnel list 2>/dev/null | awk '{print $2}' | grep -qx "$TUNNEL_NAME"; then
  echo "Tunnel '$TUNNEL_NAME' already exists; reusing it."
else
  echo "Creating tunnel '$TUNNEL_NAME'..."
  /usr/local/bin/cloudflared tunnel create "$TUNNEL_NAME"
fi

UUID="$(/usr/local/bin/cloudflared tunnel list 2>/dev/null \
  | awk -v n="$TUNNEL_NAME" '$2 == n {print $1}' | head -1)"

if [[ -z "$UUID" ]]; then
  echo "Could not determine the tunnel UUID. Run: cloudflared tunnel list" >&2
  exit 1
fi
echo "Tunnel UUID: $UUID"

# The ingress list is ordered and the last rule must be a catch-all, or
# cloudflared refuses to start.
cat > "$CF_DIR/config.yml" <<YAML
tunnel: $UUID
credentials-file: $CF_DIR/$UUID.json

ingress:
  - hostname: $DOMAIN
    service: http://127.0.0.1:$PORT
  - hostname: www.$DOMAIN
    service: http://127.0.0.1:$PORT
  - service: http_status:404
YAML
echo "Wrote $CF_DIR/config.yml"

# Idempotent: re-routing an existing hostname updates the CNAME in place.
for host in "$DOMAIN" "www.$DOMAIN"; do
  echo "Routing $host ..."
  /usr/local/bin/cloudflared tunnel route dns "$TUNNEL_NAME" "$host" || \
    echo "  (already routed, or needs attention -- check the Cloudflare dashboard)"
done

cat <<MSG

Tunnel configured. Start everything with:

    scripts/serve_public.sh          # terminal 1 -- the app on :$PORT
    cloudflared tunnel run $TUNNEL_NAME   # terminal 2 -- the tunnel

Or install the launchd agents so both survive reboot -- see DEPLOY.md.
MSG
