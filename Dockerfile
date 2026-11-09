# syntax=docker/dockerfile:1
# Imagen de odds-scanner sobre Chainguard (Wolfi), mínima y sin shell.
# La API key NUNCA se copia: llega como variable de entorno en tiempo de ejecución
# (env_file en docker-compose). data/ es un volumen.
#
# Por qué no python:3.12-slim: sus paquetes base de Debian (util-linux, ncurses,
# systemd, acl, perl) traían CVEs HIGH sin parche y el programa no los usa. El
# runtime de Chainguard solo trae Python y sus librerías, sin apt, sin shell.
# Reconstruye con `docker compose build --pull` para tomar parches nuevos.

# --- etapa 1: instalar el paquete en un venv (imagen -dev, con pip) -----------
FROM cgr.dev/chainguard/python:latest-dev AS build
USER root
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /app/venv \
    && /app/venv/bin/pip install . \
    && /app/venv/bin/pip uninstall --yes pip \
    && mkdir -p /app/data

# --- etapa 2: runtime mínimo, usuario no root ---------------------------------
# Misma etiqueta base que la etapa 1, así el venv y el intérprete coinciden.
FROM cgr.dev/chainguard/python:latest AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 \
    PATH="/app/venv/bin:$PATH"
WORKDIR /app
COPY --from=build /app/venv /app/venv
COPY --from=build --chown=65532:65532 /app/data /app/data
USER 65532:65532
VOLUME ["/app/data"]
ENTRYPOINT ["odds-scanner"]
CMD ["pipeline"]
