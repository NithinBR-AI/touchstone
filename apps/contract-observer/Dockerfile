# Contract Observer — API contract drift (Touchstone App 2 · The Séance)
#
# Self-contained image: the shared core/ skeleton + this app's OpenAPI adapter
# + the shared helpers. Nothing from the database or cache apps is copied.
#
# Build from the REPO ROOT (the skeleton lives outside this folder):
#   docker build -f apps/contract-observer/Dockerfile -t touchstone-contract-observer .
#   docker run --rm -p 8002:8000 --env-file .env touchstone-contract-observer
# Env: GITHUB_TOKEN, MANTLE_API_KEY, MANTLE_BASE_URL

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
# This app's domain adapter + shared helpers
COPY adapters/__init__.py adapters/ssrf.py ./adapters/
COPY adapters/openapi ./adapters/openapi
COPY agents ./agents
# The app, the shared UI template and the repo registry
COPY apps/contract-observer ./apps/contract-observer
COPY apps/shared/ui ./apps/shared/ui
COPY repos.json ./

# Non-root. Reports and the fix cache live in apps/contract-observer/ (ephemeral on Render free tier).
RUN useradd --create-home --uid 10001 touchstone \
 && chown -R touchstone:touchstone /app/apps/contract-observer
USER touchstone

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --app-dir apps/contract-observer --host 0.0.0.0 --port \"$PORT\""]
