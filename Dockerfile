FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/boundary

RUN addgroup --system --gid 10001 boundary \
    && adduser --system --uid 10001 --ingroup boundary boundary \
    && mkdir -p /data \
    && chown boundary:boundary /data

COPY requirements-runtime.txt ./
RUN python -m pip install --no-cache-dir -r requirements-runtime.txt

COPY --chown=boundary:boundary app ./app
COPY --chown=boundary:boundary provider_service ./provider_service

USER boundary
EXPOSE 8000 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

