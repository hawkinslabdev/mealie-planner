FROM python:3.11-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt

FROM python:3.11-slim AS runtime
ARG BUILD_VERSION=dev
LABEL org.opencontainers.image.title="Mealie Planner" \
      org.opencontainers.image.description="A streamlined week-view planner for Mealie" \
      org.opencontainers.image.version="${BUILD_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    BUILD_VERSION=${BUILD_VERSION}

WORKDIR /app
COPY --from=builder /install /usr/local
COPY main.py config.py mealie.py middleware.py utils.py database.py i18n.py ./
COPY routers/ routers/
COPY templates/ templates/
COPY assets/ assets/

RUN mkdir -p /app/data /app/uploads

EXPOSE 3000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000", "--workers", "1"]
