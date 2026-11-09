FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV PORT=8000
EXPOSE 8000
# Runs the API with the executor in the same process. Set WORKER=off to run `countersigned worker` separately.
CMD ["sh", "-c", "uvicorn countersigned.api:create_app_from_env --factory --host 0.0.0.0 --port ${PORT}"]
