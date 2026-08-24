# syntax=docker/dockerfile:1
#
# BUILT AND VERIFIED. `docker build` runs clean against this file:
#   docker build -t finhm/se1-ledger-core:latest .
# Image size 50MB, Docker Engine 29.1.3 on Ubuntu 26.04 (WSL2).
#
# What that does and does not establish: the image BUILDS and the
# layers resolve. It is not a statement that the service inside it has
# been run under load, and for the four HTTP services the load numbers
# in each README were measured on the host rather than in the container.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: a source change must not invalidate this layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root. A root container that mounts a volume writes root-owned files onto
# the host, which is somebody else's afternoon.
RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health',timeout=2).status==200 else 1)"

CMD ["uvicorn", "serve:app", "--host", "0.0.0.0", "--port", "8100"]

