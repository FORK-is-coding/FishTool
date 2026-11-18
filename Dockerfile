FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock requirements.txt ./

RUN uv sync --frozen --no-dev \
    --no-install-package pyside6 \
    --no-install-package pyside6-addons \
    --no-install-package pyside6-essentials \
    --no-install-package pyside6-pdf \
    --no-install-package pyside6-webengine

COPY . .

EXPOSE 8000
VOLUME ["/app/data", "/app/config"]

CMD ["uv", "run", "--no-dev", "--no-sync", "python", "app.py", "web", "--host", "0.0.0.0", "--port", "8000"]
