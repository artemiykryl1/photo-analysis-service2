#!/bin/sh
# TASK-003 B2 (design §8.2): render config.js.template -> config.js at
# CONTAINER START via envsubst, then hand off to nginx. This is what makes
# one image work unmodified in docker-compose, on the server and in
# Kubernetes - only the env vars differ.
set -eu

: "${API_BASE_URL:=http://localhost:8000}"
: "${SHARPNESS_THRESHOLD:=100.0}"
: "${POLL_INTERVAL_MS:=3000}"

export API_BASE_URL SHARPNESS_THRESHOLD POLL_INTERVAL_MS

# Restricting the substitution list to exactly these three variables (rather
# than a bare `envsubst` with no argument) avoids accidentally mangling any
# other `${...}`/`$VAR`-looking text that might end up in the template.
envsubst '${API_BASE_URL} ${SHARPNESS_THRESHOLD} ${POLL_INTERVAL_MS}' \
    < /usr/share/nginx/html/config.js.template \
    > /usr/share/nginx/html/config.js

# `exec` replaces this shell (PID 1) with nginx, so SIGTERM from
# `docker compose stop`/Kubernetes reaches nginx directly instead of being
# swallowed by the wrapper shell (same reasoning as the api/worker `exec`
# fixes from tasks/TASK-002/40_review-1.md M7).
exec nginx -g "daemon off;"
