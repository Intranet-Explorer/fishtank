FROM python:3.12-slim

WORKDIR /opt/fishtank
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/fishtank \
    IN_CONTAINER=1

RUN pip install --no-cache-dir httpx==0.28.1

COPY agent/ /opt/fishtank/agent/

WORKDIR /workspace
CMD ["python", "-m", "agent.harness"]
