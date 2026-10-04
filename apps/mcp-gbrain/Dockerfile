# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# gbrain (garrytan/gbrain), built from source at a pinned commit, served as
# an MCP server over streamable HTTP by supergateway. The same image runs the
# gbrain CLI (autopilot, doctor, dream, ...) by overriding the command.
#
# Upstream tags only some releases, so the pin is a commit: VERSION is the
# version in that commit's package.json, DIGEST the commit itself.

# renovate: datasource=github-tags depName=garrytan/gbrain
ARG VERSION=v0.42.53.0
ARG DIGEST=814258dda67945ffec9457a1e73980e947b7e462

FROM docker.io/oven/bun:1.4.2-slim@sha256:cb3bbbb08e13a4a2ff400f24c7a2a1d5efa83f6ef8544d52d95a519631e2fc61 AS build
ARG DIGEST
ADD https://github.com/garrytan/gbrain.git#${DIGEST} /app
WORKDIR /app
# --ignore-scripts: postinstall runs database migrations when `gbrain` is on
# PATH. There is no database at build time, and migrations are an operator
# decision (`gbrain apply-migrations`), not something an image rebuild does.
RUN bun install --frozen-lockfile --production --ignore-scripts
# supergateway is pinned in its own lockfile rather than fetched with `bunx`
# at start: 4.x exits as soon as stdin closes, which is immediately in a
# container with no stdin.
COPY supergateway/package.json supergateway/bun.lock /opt/supergateway/
RUN cd /opt/supergateway && bun install --frozen-lockfile --production

FROM docker.io/oven/bun:1.4.2-slim@sha256:cb3bbbb08e13a4a2ff400f24c7a2a1d5efa83f6ef8544d52d95a519631e2fc61
LABEL org.opencontainers.image.description="gbrain built from source, served as an MCP server over HTTP by supergateway"
# git: gbrain's source of truth is a git repository. procps: `gbrain serve`
# uses ps to notice its parent exiting; without it the watchdog silently
# disables itself and orphaned children accumulate. tini: PID 1 that reaps
# the zombies supergateway's children leave behind.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git procps tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /app /app
COPY --from=build /opt/supergateway /opt/supergateway
RUN printf '#!/bin/sh\nexec bun /app/src/cli.ts "$@"\n' > /usr/local/bin/gbrain \
 && printf '#!/bin/sh\nexec bun /opt/supergateway/node_modules/supergateway/dist/index.js "$@"\n' > /usr/local/bin/supergateway \
 && chmod 755 /usr/local/bin/gbrain /usr/local/bin/supergateway
# Read-only root filesystem: nothing is installed at runtime, and bun's
# transpiler cache (which otherwise writes under $HOME) is disabled.
ENV HOME=/tmp \
    BUN_RUNTIME_TRANSPILER_CACHE_PATH=0
USER 1000:1000
EXPOSE 8080
ENTRYPOINT ["tini", "--"]
# --stateful keeps one `gbrain serve` child per MCP session, reaped after
# sessionTimeout; the stateless default starts one per request and never
# reaps them on keep-alive connections.
CMD ["supergateway", "--stdio", "gbrain serve", "--outputTransport", "streamableHttp", "--stateful", "--sessionTimeout", "600000", "--healthEndpoint", "/healthz", "--port", "8080"]
