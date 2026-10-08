# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# unifi-network-mcp: the network server from sirkirby's unifi-mcp monorepo,
# built from source at the release tag's commit. Mirrors upstream's
# apps/network/Dockerfile (same Python, workdir, port, command and workspace
# subset), but multi-stage, so uv isn't in the final image, and non-root.
# Upstream tags are per app (network/vX.Y.Z); the image tag is X.Y.Z.

# renovate: datasource=github-tags depName=sirkirby/unifi-network-mcp versioning=regex:^network/v(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)$
ARG VERSION=network/v0.32.10
ARG DIGEST=41a141afb914f7eac2e58c5d56a845db8fa18918

FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv

FROM docker.io/library/python:3.14.8-slim@sha256:89fb7d3da20043c370643435258bdd7ab755d326d359001d02988ed15ae5219e AS build
ARG VERSION
ARG DIGEST
COPY --from=uv /uv /usr/local/bin/uv
ADD https://github.com/sirkirby/unifi-network-mcp.git#${DIGEST} /src
WORKDIR /app
# The same slice of the uv workspace upstream builds: the network app and the
# two packages it depends on.
RUN cp /src/pyproject.toml /src/uv.lock ./ \
 && mkdir -p apps packages \
 && cp -r /src/apps/network apps/network \
 && cp -r /src/packages/unifi-core /src/packages/unifi-mcp-shared packages/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3
# hatch-vcs needs a version without .git; --no-editable so the venv doesn't
# point back at /app/apps and /app/packages.
RUN v="${VERSION##*/}" \
 && SETUPTOOLS_SCM_PRETEND_VERSION="${v#v}" \
    uv sync --frozen --no-dev --no-editable --package unifi-network-mcp

FROM docker.io/library/python:3.14.8-slim@sha256:89fb7d3da20043c370643435258bdd7ab755d326d359001d02988ed15ae5219e
LABEL org.opencontainers.image.description="UniFi Network MCP server, built from source"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    HOME=/tmp
USER 65532:65532
EXPOSE 3000
CMD ["unifi-network-mcp"]
