# Self-preparing OSRM image: on first boot (no compiled dataset in the
# mounted volume yet) it downloads the configured OSM region extract and
# compiles it, then starts osrm-routed. Subsequent restarts reuse the
# already-compiled data and start immediately. See docker-entrypoint.sh and
# osrm/README.md.
FROM ghcr.io/project-osrm/osrm-backend:latest

# The entrypoint downloads the extract with wget. Recent upstream images are
# Debian-based and ship neither wget nor curl (older ones were Alpine with
# busybox wget) — install it with whichever package manager the base has.
RUN if command -v apt-get >/dev/null 2>&1; then \
      apt-get update && apt-get install -y --no-install-recommends wget ca-certificates \
      && rm -rf /var/lib/apt/lists/*; \
    elif command -v apk >/dev/null 2>&1; then \
      apk add --no-cache wget ca-certificates; \
    fi

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
