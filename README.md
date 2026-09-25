# container-images

Container images built from source or thinly extended from vendor images, published to `ghcr.io/jtrusty/<name>`.

| Image | What | Built from |
| --- | --- | --- |
| `actions-runner` | Actions Runner Controller runner with a CI toolchain: Node 24, uv + Python 3.10, Go, kubectl, Playwright's Chromium system libraries | `FROM ghcr.io/actions/actions-runner` |
| `hermes-agent` | hermes-agent with the GitHub CLI and tirith preinstalled | `FROM docker.io/nousresearch/hermes-agent` |
| `mcp-authentik` | authentik MCP server | source, `Samik081/mcp-authentik` |
| `mcp-truenas` | TrueNAS MCP server | source, `cedricziel/truenas-mcp` |
| `mcp-unifi` | UniFi Network MCP server | source, `sirkirby/unifi-network-mcp` |

## Why

- **Single-maintainer images** (the MCP servers) are rebuilt **from source at a pinned commit**, so an upstream release shows up as a Renovate PR that can be read before it becomes an image.
- **Vendor images we extend** (the runner, hermes-agent) stay `FROM` the vendor's image, pinned by digest, with only what we add on top.

## Layout and conventions

One directory per image: `apps/<name>/Dockerfile` and an executable `apps/<name>/test.sh` (the PR smoke test). Every Dockerfile sets an `org.opencontainers.image.description` label, which becomes the package description, and declares its upstream at the top. Renovate updates both upstream lines together:

```dockerfile
# renovate: datasource=<docker|github-tags> depName=<image or owner/repo>
ARG VERSION=<tag>
ARG DIGEST=<sha256:… for docker, commit SHA for github-tags>
```

- The published tag is `VERSION` with any path prefix (monorepo tags such as `network/v1.2.3`) and leading `v` removed. Deploy by **digest**, never by tag.
- Source builds fetch the upstream repo at `DIGEST` (a commit), not at the tag, so a moved tag can't change what we build.

## Build and trust

`.github/workflows/build.yaml`:

- **Pull requests:** build the changed images with `packages: read` (nothing is pushed), then run each image's `apps/<name>/test.sh` smoke test against the local build. Servers are started non-root with a read-only root filesystem and must answer on their port; tool images must run their tools.
- **`main` and dispatch:** build the changed images, push `ghcr.io/jtrusty/<name>:<version>` with OCI labels and index annotations, then sign the digest with cosign (keyless) and verify the signature. Only this job holds `packages: write`.
- **No scheduled rebuild.** Everything is pinned, so a timed rebuild would only change the digest.

`.github/workflows/scan.yaml` scans every published image weekly with grype (fixable high and critical only) and reports to code scanning. A finding there is the signal to bump an image.

`.github/workflows/lint.yaml` runs [zizmor](https://github.com/zizmorcore/zizmor) on the workflows. Every action is pinned to a commit SHA, and every checkout sets `persist-credentials: false`.

Tools downloaded with a pinned checksum (`gh`, `tirith`, `kubectl`) get a `checksum-update` label on their Renovate PRs: update the `*_SHA256` argument by hand.

Verify a published image (signatures are cosign 3 bundles, so this needs **cosign 3+**):

```sh
cosign verify ghcr.io/jtrusty/<name>@sha256:<digest> \
  --certificate-identity 'https://github.com/jtrusty/container-images/.github/workflows/build.yaml@refs/heads/main' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Rules

- **`main` is protected:** changes land through a PR, and the `build-ok` check (every changed image built and smoke-tested) must pass. The publish job smoke-tests the pushed digest again before signing it.
- **Never clean up untagged image versions.** Re-publishing a tag leaves the previous digest untagged, and consumers deploy by digest, so an old digest may still be in use.

## Runbooks

**Rebuild one image** (for example after a scan finding in a base image's system packages): run the `build` workflow manually with `app` set to the image's directory name. It builds, tests, publishes and signs only that image. Consumers pick up the new digest the next time they re-pin.

**Add an image:**

1. Create `apps/<name>/Dockerfile`. Start with the pinned `# syntax=` line and the renovate/`VERSION`/`DIGEST` header, and set `LABEL org.opencontainers.image.description`. The final stage runs as a non-root user.
2. Create an executable `apps/<name>/test.sh` taking the image reference as `$1`. For a server, call `.github/scripts/smoke-http.sh` (any HTTP answer counts, it runs the image's own user, and root fails). For a tool image, run the tools.
3. Add a row to the table above, then open a PR.
