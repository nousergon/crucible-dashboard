#!/usr/bin/env bash
# Best-effort Cloudflare cache purge for Streamlit /static/* on the three
# hostnames this box serves. Invoked after a requirements.txt install (streamlit
# pin may have rotated content-hashed chunks) and by the one-shot GHA workflow.
#
# Exit 0 on success or when no usable token is configured.
# Exit 1 only when a token is present AND the purge API rejects it — so a
# mis-scoped token is loud, while a missing token is a quiet no-op (the box
# SSM parameters today are Access-scoped and cannot purge; the GHA secret can).
set -euo pipefail

Z="${CF_ZONE_ID:-6d069b20bf36cfaf5b162abeaac41e38}"
HOSTS=(console.nousergon.ai dashboard.nousergon.ai live.nousergon.ai)

TOKEN="${CLOUDFLARE_API_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  # Prefer the fleet-wide parameter; fall back to the dashboard Access token
  # (expected to lack Cache Purge — we probe and no-op cleanly).
  for NAME in /alpha-engine/CLOUDFLARE_API_TOKEN /alpha-engine/dashboard/secrets/CF_API_TOKEN; do
    TOKEN=$(aws ssm get-parameter --name "$NAME" \
      --with-decryption --query Parameter.Value --output text 2>/dev/null || true)
    [ -n "${TOKEN:-}" ] && break
  done
fi

if [ -z "${TOKEN:-}" ]; then
  echo "purge_streamlit_static_cache: no token configured — skip"
  exit 0
fi

AUTH="Authorization: Bearer ${TOKEN}"

# Probe: can this token purge? A single-file attempt is the cheapest read of
# the grant. Auth error → quiet skip (Access-only token class). Other errors
# → fail loud.
PROBE=$(curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  "https://api.cloudflare.com/client/v4/zones/${Z}/purge_cache" \
  --data '{"files":["https://console.nousergon.ai/static/js/__purge_probe__"]}')
PROBE_OK=$(echo "$PROBE" | python3 -c 'import json,sys; print("1" if json.load(sys.stdin).get("success") else "0")')
if [ "$PROBE_OK" != "1" ]; then
  ERR=$(echo "$PROBE" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("errors"))')
  case "$ERR" in
    *10000*|*Authentication*|*Unauthorized*|*9109*)
      echo "purge_streamlit_static_cache: token lacks Cache Purge — skip ($ERR)"
      exit 0
      ;;
    *)
      echo "purge_streamlit_static_cache: probe failed: $ERR" >&2
      exit 1
      ;;
  esac
fi

# Prefer hosts purge (one call). Fall back to prefixes if the plan rejects hosts.
HOSTS_JSON=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${HOSTS[@]}")
RESP=$(curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  "https://api.cloudflare.com/client/v4/zones/${Z}/purge_cache" \
  --data "{\"hosts\": ${HOSTS_JSON}}")
if echo "$RESP" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("success") else 1)'; then
  echo "purge_streamlit_static_cache: hosts purged OK"
  exit 0
fi

PREFIXES='["console.nousergon.ai/static","dashboard.nousergon.ai/static","live.nousergon.ai/static"]'
RESP=$(curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  "https://api.cloudflare.com/client/v4/zones/${Z}/purge_cache" \
  --data "{\"prefixes\": ${PREFIXES}}")
if echo "$RESP" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("success") else 1)'; then
  echo "purge_streamlit_static_cache: prefixes purged OK"
  exit 0
fi

echo "purge_streamlit_static_cache: hosts+prefixes both failed: $RESP" >&2
exit 1
