# syntax=docker/dockerfile:1
# Imagen de odds-scanner. La API key NUNCA se copia: llega como variable de
# entorno en tiempo de ejecución (env_file en docker-compose). data/ es un volumen.

# --- etapa 1: construir la wheel ---------------------------------------------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels .

# --- etapa 2: runtime mínimo, usuario no root --------------------------------
FROM python:3.12-slim AS runtime
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8
# Aplica los parches de seguridad de Debian publicados después de la imagen base.
RUN apt-get update     && apt-get upgrade --yes --no-install-recommends     && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 scanner \
    && useradd --system --uid 10001 --gid scanner --home-dir /app --shell /usr/sbin/nologin scanner
COPY --from=build /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels
WORKDIR /app
RUN mkdir -p /app/data && chown scanner:scanner /app/data
USER scanner
VOLUME ["/app/data"]
ENTRYPOINT ["odds-scanner"]
CMD ["pipeline"]
