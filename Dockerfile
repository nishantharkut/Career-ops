FROM node:24-slim AS web

WORKDIR /app
COPY apps/web/package*.json apps/web/
RUN npm --prefix apps/web ci
COPY apps/web apps/web
RUN npm --prefix apps/web run build

FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    CAREEROPS_DB_PATH=/app/data/careerops.db \
    CAREEROPS_VAULT_PATH=/app/vault \
    CAREEROPS_REQUIRE_AUTH=true

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        latexmk \
        texlive-fonts-recommended \
        texlive-latex-base \
        texlive-latex-recommended \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY apps apps
COPY careerops careerops
COPY --from=web /app/apps/web/dist apps/web/dist

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data /app/vault

EXPOSE 8765

CMD ["python", "-m", "uvicorn", "apps.api.careerops_api.main:app", "--host", "0.0.0.0", "--port", "8765"]
