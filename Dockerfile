# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Stage 1: build wheel (and resolve deps) using uv for fast cold builds.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src

COPY pyproject.toml README.md ./
COPY charter ./charter

RUN pip install --upgrade pip build && \
    python -m build --wheel --outdir /wheels

# ---------------------------------------------------------------------------
# Stage 2: runtime image. Minimal — only the wheel and its runtime deps.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    CHARTER_DATA_DIR=/data \
    CHARTER_PORT=8000

# Run as non-root.
RUN groupadd --system charter && \
    useradd  --system --gid charter --home-dir /home/charter --create-home charter

WORKDIR /app

COPY --from=build /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels

# Charter writes signed Charters and Ed25519 keys here. Mount a volume in
# production so they survive container restarts.
RUN mkdir -p /data && chown -R charter:charter /data /app

USER charter

EXPOSE 8000

# Liveness/readiness checks hit /healthz inside the container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

# The charter-server console script wraps uvicorn with the right binding.
CMD ["charter-server"]
