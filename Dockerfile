# ── Base image ────────────────────────────────────────────────────────────────
# python:3.12-slim is a minimal Debian image with Python pre-installed.
# "slim" keeps the image small by excluding documentation and optional packages.
FROM python:3.12-slim

# ── System dependencies ───────────────────────────────────────────────────────
# gcc            — needed to compile Python packages with C extensions (asyncpg).
# postgresql-client — provides the `psql` binary, used by setup mode in
#                  docker-entrypoint.sh to apply migrations against the metadata DB.
# --no-install-recommends keeps the layer lean. The apt cache is purged immediately.
RUN apt-get update && apt-get install -y --no-install-recommends gcc postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
# All subsequent commands and file copies are relative to /app inside the image.
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
# We install CPU-only PyTorch BEFORE requirements.txt.
# Why: sentence-transformers pulls in torch automatically, and by default pip
# would download the GPU version (~3 GB). Installing CPU-only torch first
# (~700 MB) tells pip "torch is already satisfied" and skips the GPU build.
RUN pip install --no-cache-dir torch --extra-index-url https://download.pytorch.org/whl/cpu

# Copy requirements.txt first (before the rest of the code).
# Docker caches each layer. If requirements.txt hasn't changed, this expensive
# pip install step is skipped on the next build — saving several minutes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
# Source code, migrations, and the entrypoint script. env/ is excluded by
# .dockerignore — secrets are injected via environment variables at runtime,
# not baked into the image.
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# ── Port ─────────────────────────────────────────────────────────────────────
# Documents which port the app listens on. This does NOT publish the port
# to the host — that's done in docker-compose.yml.
EXPOSE 8080

# ── Entrypoint ────────────────────────────────────────────────────────────────
# The entrypoint script switches behaviour on the first argument:
#   setup  — apply migrations + run all indexing scripts (one-shot)
#   api    — run uvicorn (default; long-running)
# Override at run time, e.g. `docker compose run --rm api setup`.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["api"]
