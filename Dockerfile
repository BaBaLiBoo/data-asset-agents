FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DEFAULT_TIMEOUT=300
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip pip install --retries 10 .

COPY apps ./apps
COPY ontology ./ontology
COPY data ./data
COPY scripts ./scripts

ARG GIT_COMMIT_SHA=unknown
ENV GITHUB_SHA=${GIT_COMMIT_SHA}

EXPOSE 8000 8501
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
