#!/usr/bin/env bash
# One short command instead of a long one to copy out of a terminal that only
# hands you the first word. Every operation the live site needs, by name.
#
#   scripts/atlas.sh status     what is up, what is not
#   scripts/atlas.sh flushdns   clear the stale NXDOMAIN for the apex
#   scripts/atlas.sh start      load both launchd agents
#   scripts/atlas.sh stop       unload both
#   scripts/atlas.sh restart    unload then load
#   scripts/atlas.sh logs       tail app and tunnel logs together
#   scripts/atlas.sh awake      stop the Mac sleeping for 4 hours

set -uo pipefail

DOMAIN="agreementatlas.com"
PORT="${PUBLIC_PORT:-8001}"
APP="com.agreementatlas.app"
TUNNEL="com.agreementatlas.tunnel"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs"

ok()   { printf "  \033[32m OK \033[0m %s\n" "$1"; }
bad()  { printf "  \033[31mFAIL\033[0m %s\n" "$1"; }
warn() { printf "  \033[33mWARN\033[0m %s\n" "$1"; }

code() { curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$1" 2>/dev/null; }

status() {
  echo "AgreementAtlas — $DOMAIN"
  echo

  # Read the job list once. `launchctl list | grep -q` looks right and is not:
  # grep exits on the first match, launchctl takes SIGPIPE, and under pipefail
  # the pipeline reports 141 -- so whichever agent is listed first reads as
  # missing while it is in fact running.
  local jobs; jobs=$(launchctl list)
  case "$jobs" in *"$APP"*)    ok "app agent loaded"    ;; *) bad "app agent NOT loaded"    ;; esac
  case "$jobs" in *"$TUNNEL"*) ok "tunnel agent loaded" ;; *) bad "tunnel agent NOT loaded" ;; esac

  local local_code; local_code=$(code "http://127.0.0.1:$PORT/")
  [ "$local_code" = "200" ] && ok "app answering on :$PORT" || bad "app not answering on :$PORT (got $local_code)"

  # The whole point of public-demo mode: the shared library, which holds real
  # licensed agreements, must not be reachable.
  local lib; lib=$(code "http://127.0.0.1:$PORT/api/families")
  [ "$lib" = "404" ] && ok "library disabled (404) — licensed docs not exposed" \
                     || bad "library returned $lib — EXPECTED 404, stop the tunnel"

  local lm; lm=$(code "http://127.0.0.1:1234/v1/models")
  [ "$lm" = "200" ] && ok "LM Studio answering" \
                    || bad "LM Studio down — site loads but every question fails"

  # Two settings that are harmless on a laptop and are not harmless once a URL
  # is on a job application. Checked here because neither of us will remember
  # them in a week.
  local lmcfg="$HOME/.lmstudio/.internal/http-server-config.json"
  if [ -f "$lmcfg" ]; then
    # Bound to 0.0.0.0, LM Studio answers anyone on the same wifi -- they can
    # drive the GPU and load models. The tunnel does not expose it; the network
    # you are sitting on does.
    if grep -q '"networkInterface": *"127.0.0.1"' "$lmcfg"; then
      ok "LM Studio bound to localhost"
    else
      local bind; bind=$(sed -n 's/.*"networkInterface": *"\([^"]*\)".*/\1/p' "$lmcfg")
      warn "LM Studio open to your local network (bind=${bind:-unknown})"
      printf "         fix: LM Studio → Developer → Server Settings → serve on localhost only\n"
    fi

    # logSensitiveData writes whole prompts to disk. A prompt here is somebody
    # else's uploaded contract, and the privacy notice tells them it is gone
    # when the session expires.
    if grep -q '"logSensitiveData": *false' "$lmcfg"; then
      ok "LM Studio not logging prompt contents"
    else
      local size; size=$(du -sh "$HOME/.lmstudio/server-logs" 2>/dev/null | cut -f1)
      warn "LM Studio logging full prompts to disk (${size:-unknown}) — uploaded agreements are retained"
      printf "         fix: LM Studio → Developer → Server Settings → turn off verbose/sensitive logging\n"
    fi
  fi

  # Public resolvers are the ones that matter; this Mac's cache is not evidence.
  if dig +short @1.1.1.1 "$DOMAIN" | grep -q .; then
    ok "public DNS resolves $DOMAIN"
  else
    bad "public DNS does not resolve $DOMAIN"
  fi

  if dig +short "$DOMAIN" | grep -q .; then
    ok "this Mac resolves $DOMAIN"
  else
    warn "this Mac has a stale cache — run: scripts/atlas.sh flushdns"
  fi

  local live; live=$(code "https://$DOMAIN/")
  [ "$live" = "200" ] && ok "https://$DOMAIN → 200" || warn "https://$DOMAIN → $live"

  if pmset -g | grep -qE "^\s+sleep\s+0"; then
    ok "system sleep disabled"
  else
    warn "system will sleep — the site dies with it; run: scripts/atlas.sh awake"
  fi
}

case "${1:-status}" in
  status)  status ;;
  flushdns)
    echo "Flushing DNS (you will be asked for your password)…"
    sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder && echo "done."
    ;;
  start)
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$APP.plist" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$TUNNEL.plist" 2>/dev/null
    sleep 3; status
    ;;
  stop)
    launchctl bootout "gui/$(id -u)/$TUNNEL" 2>/dev/null
    launchctl bootout "gui/$(id -u)/$APP" 2>/dev/null
    echo "stopped."
    ;;
  restart) "$0" stop; sleep 2; "$0" start ;;
  logs)    tail -f "$LOGS/agreementatlas-app.log" "$LOGS/agreementatlas-tunnel.log" ;;
  awake)
    echo "Preventing sleep for 4 hours. Close this window to cancel."
    caffeinate -dims -t 14400
    ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
