# RunLedger team server image.
#
# Build from the repository root:  docker build -t runledger .
# Or use deploy/docker-compose.yml, which adds Caddy for HTTPS.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="RunLedger" \
      org.opencontainers.image.description="RunLedger team server: collects agent run receipts and serves a dashboard" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Unprivileged user. The only path it can write to is /data.
RUN groupadd --system runledger \
 && useradd --system --gid runledger --home-dir /data --shell /usr/sbin/nologin runledger \
 && mkdir /data \
 && chown runledger:runledger /data

# Install from source, then remove the sources from the image.
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY runledger ./runledger
RUN pip install --no-cache-dir . \
 && cd / \
 && rm -rf /build

# The working directory is the volume, so `runledger team create NAME` run in the
# container writes its database to /data, next to the server's database.
WORKDIR /data
VOLUME /data
USER runledger
EXPOSE 8787

# Probes the port used by the CMD below. Exec form, so no shell is involved.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["runledger"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8787", "--db", "/data/runledger.db", "--trust-proxy"]
