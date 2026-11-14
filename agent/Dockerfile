# syntax=docker/dockerfile:1
# Build context: repository root.
FROM python:3.15.0-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY agent/requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.15.0-slim
ARG RELEASE_VERSION=dev
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RELEASE_VERSION=${RELEASE_VERSION} \
    PORT=8080
# /data holds the SQLite store. Created here, owned by the app user, so the
# named volume mounted over it inherits that ownership.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app \
    && mkdir /data \
    && chown 10001:10001 /data
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY services/__init__.py services/__init__.py
COPY services/kit services/kit
COPY libs libs
COPY agent agent
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-m", "services.kit.healthcheck"]
CMD ["python", "-m", "agent"]
