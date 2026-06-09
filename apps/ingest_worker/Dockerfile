FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY apps/ingest_worker ./apps/ingest_worker
RUN pip install --no-cache-dir -e .

CMD ["python", "apps/ingest_worker/main.py"]
