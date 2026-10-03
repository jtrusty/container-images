# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# A narrow HTTP front for launching Dagster runs: callers pick one of a fixed
# set of targets and read back only the runs it started. The service is in
# this directory (standard library only), so there is no upstream: VERSION is
# this image's own, bumped by hand when launcher.py changes. See README.md.

ARG VERSION=0.4.0

FROM gcr.io/distroless/python3-debian12:nonroot@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5
LABEL org.opencontainers.image.description="Constrained launcher for Dagster runs: allowlisted targets, typed parameters, one active run, status of its own runs only"
COPY launcher.py selftest.py /app/
# No .pyc writes: the root filesystem is read-only.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["python3", "/app/launcher.py"]
