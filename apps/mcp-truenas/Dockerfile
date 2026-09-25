# syntax=docker/dockerfile:1.27.0@sha256:bde3983e9c939224420ddaf6b784cc30e09b035a4dea01f581230c50809f372e
# cedricziel/truenas-mcp built from source at the release tag's commit.
# Mirrors upstream's own Dockerfile: static Go binary on
# distroless static (CA certificates for the target's TLS, nonroot, no shell).

# renovate: datasource=github-tags depName=cedricziel/truenas-mcp
ARG VERSION=v0.4.2
ARG DIGEST=cd66fba8b6b3547c8cf202a225bdafd416cc631d

FROM docker.io/library/golang:1.26.5-alpine@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 AS build
ARG VERSION
ARG DIGEST
ARG TARGETOS
ARG TARGETARCH
ADD https://github.com/cedricziel/truenas-mcp.git#${DIGEST} /src
WORKDIR /src
RUN go mod download
# Upstream's release build injects the version without the leading v.
RUN CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags "-s -w -X main.version=${VERSION#v}" \
    -o /out/truenas-mcp ./cmd/truenas-mcp

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
LABEL org.opencontainers.image.description="TrueNAS MCP server, built from source"
LABEL io.modelcontextprotocol.server.name="io.github.cedricziel/truenas-mcp"
COPY --from=build /out/truenas-mcp /usr/local/bin/truenas-mcp
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/truenas-mcp"]
