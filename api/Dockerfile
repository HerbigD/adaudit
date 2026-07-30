FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "python-multipart>=0.0.9" \
      "pydantic>=2.7" "pydantic-settings>=2.3" "httpx>=0.27" \
      "langgraph>=1.0" "langgraph-checkpoint-sqlite>=3.0" "aiosqlite>=0.20"

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
