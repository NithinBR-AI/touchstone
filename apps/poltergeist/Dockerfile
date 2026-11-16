# The Poltergeist — cache drift (Touchstone App 3)
#
# Self-contained image: the shared core/ skeleton + this app's cache adapter
# + the shared helpers. No database, API-contract or LLM code is used —
# cache fixes are deterministic.
#
# Build from the REPO ROOT (the skeleton lives outside this folder):
#   docker build -f apps/poltergeist/Dockerfile -t touchstone-poltergeist .
#   docker run --rm -p 8003:8000 touchstone-poltergeist
# Needs no secrets: it reads and fixes the cache through the demo target API.

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
COPY adapters/redis ./adapters/redis
COPY agents ./agents
# The app, the shared UI template and the repo registry
COPY apps/poltergeist ./apps/poltergeist
COPY apps/shared/ui ./apps/shared/ui
COPY repos.json ./

# Non-root. Reports and the fix cache live in apps/poltergeist/ (ephemeral on Render free tier).
RUN useradd --create-home --uid 10001 touchstone \
 && chown -R touchstone:touchstone /app/apps/poltergeist
USER touchstone

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --app-dir apps/poltergeist --host 0.0.0.0 --port \"$PORT\""]
