# ── Base image ────────────────────────────────────────────────────────────────
# python:3.12-slim is a minimal Debian image with Python pre-installed.
# "slim" keeps the image small by excluding documentation and optional packages.
FROM python:3.12-slim

# ── System dependencies ───────────────────────────────────────────────────────
# gcc is required to compile some Python packages that include C extensions
# (asyncpg in particular). --no-install-recommends keeps the layer lean.
# We delete the apt cache immediately so it doesn't bloat the image.
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
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
# We copy only the source code. env/ is excluded by .dockerignore — secrets
# are injected via environment variables at runtime, not baked into the image.
COPY src/ ./src/

# ── Port ─────────────────────────────────────────────────────────────────────
# Documents which port the app listens on. This does NOT publish the port
# to the host — that's done in docker-compose.yml.
EXPOSE 8080

# ── Startup command ───────────────────────────────────────────────────────────
# api.py uses bare imports ("from auth import ...") that work only when the
# working directory IS src/. We set WORKDIR here so uvicorn runs from the
# right place.
WORKDIR /app/src

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
