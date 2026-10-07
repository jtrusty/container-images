# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# mcp-duckdb: motherduckdb/mcp-server-motherduck (DuckDB MCP over Streamable
# HTTP), built from source at the release tag's commit, multi-stage and
# non-root, with the httpfs, postgres and ducklake extensions baked in.
#
# Baked because a locked-down deployment (enable_external_access=false,
# lock_configuration=true in --init-sql) can't download extensions at
# runtime: --init-sql should start with
#   SET extension_directory = '/opt/duckdb/extensions';
#   LOAD httpfs; LOAD postgres; LOAD ducklake;
# The extensions are built for the duckdb version this release pins.

# renovate: datasource=github-tags depName=motherduckdb/mcp-server-motherduck
ARG VERSION=v1.1.0
ARG DIGEST=41acb581d9bc04e427655ba5178b2c5c01f68cd2

FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv

FROM docker.io/library/python:3.14.8-slim@sha256:89fb7d3da20043c370643435258bdd7ab755d326d359001d02988ed15ae5219e AS build
ARG DIGEST
COPY --from=uv /uv /usr/local/bin/uv
ADD https://github.com/motherduckdb/mcp-server-motherduck.git#${DIGEST} /src
WORKDIR /app
RUN cp /src/pyproject.toml /src/uv.lock /src/README.md ./ && cp -r /src/src ./src
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3
RUN uv sync --frozen --no-dev --no-editable
# Signed core extensions for the pinned duckdb, so nothing is fetched at runtime.
RUN /app/.venv/bin/python -c "import duckdb; \
c = duckdb.connect(config={'extension_directory': '/opt/duckdb/extensions'}); \
[c.install_extension(e) for e in ('httpfs', 'postgres', 'ducklake')]; \
[c.load_extension(e) for e in ('httpfs', 'postgres', 'ducklake')]; \
print(c.execute(\"select extension_name, extension_version from duckdb_extensions() where installed\").fetchall())"

FROM docker.io/library/python:3.14.8-slim@sha256:89fb7d3da20043c370643435258bdd7ab755d326d359001d02988ed15ae5219e
LABEL org.opencontainers.image.description="DuckDB MCP server (mcp-server-motherduck), built from source, with httpfs, postgres and ducklake extensions baked in"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /opt/duckdb /opt/duckdb
ENV PATH="/app/.venv/bin:${PATH}" \
    HOME=/tmp \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["mcp-server-motherduck"]
CMD ["--transport", "http", "--db-path", ":memory:", "--read-write"]
