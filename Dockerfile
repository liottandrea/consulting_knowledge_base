# syntax=docker/dockerfile:1
#
# Image for the KB pipeline. Runs the MCP server as a long-lived service and is
# also used for one-off ingest/update/embed commands:
#     docker compose run --rm mcp-server python update_kb.py
#     docker compose run --rm mcp-server python embed.py
# Full python image (not slim) so the native libs onnxruntime/torch need
# (libgomp, glib) are already present — this image is built apt-free, because
# in some corporate networks an intercepting proxy blocks apt package downloads
# (the https->http redirect apt refuses). pip/uv installs still work, so the
# only OS lib we'd otherwise need (libGL, for OpenCV's GUI build) is avoided by
# swapping OpenCV to its headless build below.

FROM python:3.12

# uv for fast, reproducible installs straight from uv.lock.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Node runtime, vendored from the official image (keeps the build apt-free — some
# corporate proxies block apt over the https->http redirect). We copy only the
# node binary + npm and recreate the CLI symlinks; qmd itself is installed below,
# in THIS python:3.12 image, because qmd has native deps (tree-sitter, node-gyp)
# that need Python + a C toolchain — both already present here (python:3.12 is
# buildpack-deps-based), but absent from node:*-slim.
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Corporate TLS-intercepting proxy (Zscaler etc.) support. If your network MITMs
# HTTPS, the build container must trust the corporate root CA or every package
# download (uv/pip, and runtime model downloads) fails TLS validation. Drop the
# cert(s) as *.crt into ./certs/ and they are installed into the system trust
# store here. Harmless when ./certs/ holds only .gitkeep (no proxy).
COPY certs/ /usr/local/share/ca-certificates/corp/
RUN update-ca-certificates || true

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NATIVE_TLS=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/cache/huggingface \
    QMD_HOME=/app/.qmd \
    PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# Install qmd globally now that Node, Python, the C toolchain, and the corporate
# CA are all in place. node-gyp compiles qmd's native deps (tree-sitter) against
# this image's Python; NODE_EXTRA_CA_CERTS lets npm's registry TLS pass a proxy.
RUN npm install -g @tobilu/qmd

WORKDIR /app

# Install dependencies first for better layer caching (only re-runs when the
# lockfile changes). The project itself is run as plain scripts, not installed.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Swap OpenCV (pulled in by RapidOCR) for its headless build, which has no libGL
# dependency. Image-only change — local installs keep the standard opencv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip uninstall opencv-python \
    && uv pip install opencv-python-headless

# Application code.
COPY *.py ./

# Default: run the MCP server (transport chosen via MCP_TRANSPORT env).
CMD ["python", "mcp_server.py"]
