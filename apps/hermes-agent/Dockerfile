# syntax=docker/dockerfile:1
# Upstream hermes-agent, pinned by digest, plus two tools it would otherwise
# fetch at runtime:
# - gh (GitHub CLI), so it needn't be downloaded at start-up.
# - tirith, hermes' command scanner. Without it on PATH, hermes downloads the
#   latest release into $HERMES_HOME/bin at runtime; baked in, the version is
#   pinned and updated through Renovate.
# Both are checksum-verified and installed to /usr/local/bin. Upstream's user,
# entrypoint, cmd and workdir are unchanged. linux/amd64 only.

# renovate: datasource=docker depName=docker.io/nousresearch/hermes-agent
ARG VERSION=v2026.9.24
ARG DIGEST=sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7

FROM docker.io/nousresearch/hermes-agent:${VERSION}@${DIGEST}
LABEL org.opencontainers.image.description="hermes-agent with the GitHub CLI and tirith preinstalled"

# renovate: datasource=github-releases depName=cli/cli
ARG GH_VERSION=2.101.0
ARG GH_SHA256=9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8
# renovate: datasource=github-releases depName=sheeki03/tirith
ARG TIRITH_VERSION=0.4.2
ARG TIRITH_SHA256=efa6bf414a83dba385d4f13137e8677f850ced9102fe74ebb14c72f31df0dc77

# Upstream already runs as root (its entrypoint drops privileges itself).
USER root

RUN set -eux \
 && curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
 && echo "${GH_SHA256}  /tmp/gh.tar.gz" | sha256sum -c - \
 && curl -fsSL -o /tmp/tirith.tar.gz \
      "https://github.com/sheeki03/tirith/releases/download/v${TIRITH_VERSION}/tirith-x86_64-unknown-linux-gnu.tar.gz" \
 && echo "${TIRITH_SHA256}  /tmp/tirith.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/gh.tar.gz -C /tmp \
 && install -m 0755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh \
 && mkdir /tmp/tirith && tar -xzf /tmp/tirith.tar.gz -C /tmp/tirith \
 && install -m 0755 /tmp/tirith/tirith /usr/local/bin/tirith \
 && rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_amd64" /tmp/tirith.tar.gz /tmp/tirith \
 && gh --version \
 && tirith --version
