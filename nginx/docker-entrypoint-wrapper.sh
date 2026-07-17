#!/bin/sh
# nginx/docker-entrypoint-wrapper.sh
# Adapted pattern — no single canonical source; assembled from the documented
# jonasal/nginx-certbot startup contract (github.com/JonasAlfredsson/docker-nginx-certbot)
# + this project's D-01/D-02 constraint that the https service must be a safe
# no-op when DOMAIN is unset.
set -e

if [ -z "$DOMAIN" ]; then
  echo "[nginx-https] DOMAIN not set — https edge intentionally idle (dev/no-domain mode)."
  # Do NOT exec the real jonasal start.sh (it would try to obtain a cert for
  # an empty server_name and fail hard). Sleep forever so `docker compose up`
  # still succeeds and the container shows healthy-but-idle in `docker compose ps`.
  exec sleep infinity
fi

# Render the site config from the template now that DOMAIN is known.
envsubst '${DOMAIN}' < /etc/nginx/templates/https.conf.template \
  > /etc/nginx/user_conf.d/miniapp.conf

# Hand off to the image's own entrypoint, which owns the full cert
# bootstrap/issuance/renewal/reload lifecycle from here.
exec /scripts/start_nginx_certbot.sh "$@"
