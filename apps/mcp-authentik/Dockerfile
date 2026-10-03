# syntax=docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
# Samik081/mcp-authentik, built from source at the release tag's commit.
# Mirrors upstream's Dockerfile: same entrypoint, port,
# working dir and uid (1001). The runtime user is numeric so
# runAsNonRoot can verify it, and the final stage carries no npm/yarn.

# renovate: datasource=github-tags depName=Samik081/mcp-authentik
ARG VERSION=v0.8.1
ARG DIGEST=01e26e633491d8ab11496ee3eb2cb9ee56edb207

FROM docker.io/library/node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS build
ARG DIGEST
ADD --keep-git-dir=false https://github.com/Samik081/mcp-authentik.git#${DIGEST} /src
WORKDIR /src
RUN npm ci \
 && npm run build \
 && npm prune --omit=dev

FROM docker.io/library/node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1
LABEL org.opencontainers.image.description="authentik MCP server, built from source"
ENV NODE_ENV=production \
    MCP_PORT=3000 \
    MCP_HOST=0.0.0.0
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
      /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
      /opt/yarn-* /usr/local/bin/yarn /usr/local/bin/yarnpkg \
 && addgroup -g 1001 -S mcp && adduser -u 1001 -S mcp -G mcp
WORKDIR /app
COPY --from=build /src/package.json /src/package-lock.json ./
COPY --from=build /src/node_modules ./node_modules
COPY --from=build /src/dist ./dist
USER 1001:1001
EXPOSE 3000
LABEL io.modelcontextprotocol.server.name="io.github.Samik081/mcp-authentik"
ENTRYPOINT ["node", "dist/index.js"]
