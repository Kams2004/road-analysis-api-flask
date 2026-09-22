# Self-preparing OSRM image: on first boot (no compiled dataset in the
# mounted volume yet) it downloads the configured OSM region extract and
# compiles it, then starts osrm-routed. Subsequent restarts reuse the
# already-compiled data and start immediately. See docker-entrypoint.sh and
# osrm/README.md.
FROM ghcr.io/project-osrm/osrm-backend:latest

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
