# syntax=docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
# Actions Runner Controller runner image. The upstream image is minimal; this
# adds a CI toolchain so jobs don't download it on every run:
# - Node 24 with npm, and corepack enabled (pnpm/yarn shims, a pinned pnpm
#   pre-downloaded), for JavaScript/TypeScript jobs
# - uv, with a managed CPython 3.10 (exact patch pinned) preinstalled
# - Go
# - kubectl, checksum-verified
# - the system libraries Playwright's Chromium needs, so browser tests can
#   run on self-hosted runners. Chromium itself is still downloaded per job
#   (`playwright install chromium`), so the image doesn't have to track each
#   project's Playwright version.
# git comes with the upstream image.

# renovate: datasource=docker depName=ghcr.io/actions/actions-runner
ARG VERSION=2.337.0
ARG DIGEST=sha256:e5496277be5d09bc968b3d64911b74e219ac4a3f2edce956a3ecf9271bea1ef4

FROM docker.io/library/node:24.21.0-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS node
FROM docker.io/library/golang:1.27.1-bookworm@sha256:69a7b9788769bec032d238959b61854e9ae87f57be9029ec04e9885fabf99195 AS go
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM ghcr.io/actions/actions-runner:${VERSION}@${DIGEST}
LABEL org.opencontainers.image.description="Actions Runner Controller runner with Node, uv/Python, Go, kubectl and Playwright's Chromium libraries"

# renovate: datasource=npm depName=playwright
ARG PLAYWRIGHT_VERSION=1.63.0
# renovate: datasource=github-releases depName=kubernetes/kubernetes
ARG KUBECTL_VERSION=v1.37.1
ARG KUBECTL_SHA256=65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8
# Exact patch, so a rebuild can't silently change the interpreter.
# renovate: datasource=python-version depName=python
ARG PYTHON_VERSION=3.10.21

USER root

# Node: the binary and its bundled npm/corepack, not the rest of the node image.
# `corepack enable` runs here because its pnpm/yarn shims go in root-owned
# /usr/local/bin; a job running it as the runner user can't write there.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node /usr/local/include/node /usr/local/include/node
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
 && node --version && npm --version \
 && corepack enable
# Jobs never stop for corepack's "download this package manager?" prompt.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0

# Go toolchain. GOTOOLCHAIN=auto (the default) still fetches a newer toolchain
# if a go.mod asks for one.
COPY --from=go /usr/local/go /usr/local/go
ENV PATH=/usr/local/go/bin:${PATH}

# uv, plus a managed Python in a shared, world-readable location so `uv sync`
# finds it without a download.
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN uv python install "${PYTHON_VERSION}" \
 && chmod -R a+rX /opt/uv \
 && uv python list --only-installed

# kubectl
RUN curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && echo "${KUBECTL_SHA256}  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/kubectl \
 && kubectl version --client

# Playwright's Chromium system libraries, installed with the Node above, plus
# libatomic, which prebuilt Node-ecosystem binaries (e.g. pnpm's standalone
# build) link against.
RUN npm_config_cache=/tmp/npm npx --yes "playwright@${PLAYWRIGHT_VERSION}" install-deps chromium \
 && apt-get install -y --no-install-recommends libatomic1 \
 && rm -rf /var/lib/apt/lists/* /tmp/npm

USER runner
# Pre-download pnpm into the runner user's corepack cache, so a project that
# pins this version in `packageManager` starts without a download.
# renovate: datasource=npm depName=pnpm
ARG PNPM_VERSION=10.34.5
RUN corepack install --global "pnpm@${PNPM_VERSION}" && pnpm --version
# Everything must work for the unprivileged runner user, not just root.
RUN node --version && go version && git --version && kubectl version --client \
 && uv python find "${PYTHON_VERSION}"
