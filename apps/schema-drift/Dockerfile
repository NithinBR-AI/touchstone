# Schema Drift — database schema drift (Touchstone App 1 · The Crypt)
#
# Self-contained image: the shared core/ skeleton + this app's Postgres adapter
# + the shared helpers. Nothing from the API-contract or cache apps is copied.
#
# Build from the REPO ROOT (the skeleton lives outside this folder):
#   docker build -f apps/schema-drift/Dockerfile -t touchstone-schema-drift .
#   docker run --rm -p 8001:8000 --env-file .env touchstone-schema-drift
# Env: DATABASE_URL, MANTLE_API_KEY, MANTLE_BASE_URL, CONTEXT7_API_KEY (optional)

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    PORT=8000

WORKDIR /app

# Dependencies, read from pyproject.toml so pinned versions have one source of truth.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# The skeleton
COPY core ./core
# This app's domain adapter + shared helpers (remote_source downloads models.py)
COPY adapters/__init__.py adapters/ssrf.py adapters/remote_source.py ./adapters/
COPY adapters/postgres ./adapters/postgres
COPY agents ./agents
# The app, the shared UI template, the repo registry and the demo seed (Reset Demo)
COPY apps/schema-drift ./apps/schema-drift
COPY apps/shared/ui ./apps/shared/ui
COPY repos.json ./
COPY fixtures/seed.py ./fixtures/seed.py

# Non-root. Reports and the fix cache live in apps/schema-drift/ (ephemeral on Render free tier).
RUN useradd --create-home --uid 10001 touchstone \
 && chown -R touchstone:touchstone /app/apps/schema-drift
USER touchstone

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --app-dir apps/schema-drift --host 0.0.0.0 --port \"$PORT\""]
