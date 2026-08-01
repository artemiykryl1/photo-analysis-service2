# TASK-003 B2 (tasks/TASK-003/20_design.md §8.1-8.2): a single static image,
# used unmodified in both docker-compose and Kubernetes. The API address and
# a couple of other runtime knobs are injected at CONTAINER START via
# envsubst (docker-entrypoint.sh), never baked in at build time - so the
# exact same image works against `http://localhost:8000` locally,
# `http://45.132.19.101:51101` on the server, or a Kubernetes ConfigMap
# value, without a rebuild.
FROM nginx:alpine

# `envsubst` is not guaranteed to be present in the base nginx:alpine image
# in every version; installing gettext explicitly makes the build
# reproducible regardless of the exact base tag.
RUN apk add --no-cache gettext

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY html/ /usr/share/nginx/html/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
