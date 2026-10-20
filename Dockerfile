# RunLedger team server image.
#
# Build from the repository root:  docker build -t runledger .
# Or use deploy/docker-compose.yml, which adds Caddy for HTTPS.
# The image carries OCI labels; pass the version and the commit you built for real values:
#   docker build -t runledger --build-arg RUNLEDGER_VERSION=0.7.0 \
#     --build-arg RUNLEDGER_REVISION="$(git rev-parse HEAD)" .

# The official Python image can also be fetched from a registry mirror when
# Docker Hub rate-limits anonymous CI runners. Default remains Docker Hub.
ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

# Build arguments for the OCI labels below. The CI docker job passes the real version from
# pyproject.toml and the commit it built; a plain `docker build` gets the placeholders.
ARG RUNLEDGER_VERSION=dev
ARG RUNLEDGER_REVISION=

LABEL org.opencontainers.image.title="RunLedger" \
      org.opencontainers.image.description="RunLedger team server: collects agent run receipts and serves a dashboard" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${RUNLEDGER_VERSION}" \
      org.opencontainers.image.revision="${RUNLEDGER_REVISION}" \
      org.opencontainers.image.source="https://github.com/kodji7202-code/runledger" \
      org.opencontainers.image.url="https://runledger.site"

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
# --trust-proxy honours X-Forwarded-Proto from any address until a trusted proxy is named.
# Recommended with deploy/docker-compose.yml: also name the Compose network's subnet, with
# --trusted-proxy <subnet> here or with RUNLEDGER_TRUSTED_PROXIES in the compose environment.
# See docs/self-hosting.md ("Trusted proxy on the Compose network").
CMD ["serve", "--host", "0.0.0.0", "--port", "8787", "--db", "/data/runledger.db", "--trust-proxy"]
