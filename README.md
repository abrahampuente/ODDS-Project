# odds-scanner · Escáner de cuotas deportivas

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Typer](https://img.shields.io/badge/CLI-Typer-009485)
![SQLite](https://img.shields.io/badge/SQLite-ledger-003B57?logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-multi--stage%20%C2%B7%20no%20root-2496ED?logo=docker&logoColor=white)
![mypy](https://img.shields.io/badge/mypy-strict-2A6DB2)
![Coverage](https://img.shields.io/badge/coverage-%E2%89%A585%25-brightgreen)
![CI](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white)

Herramienta de línea de comandos en Python que consulta cuotas deportivas a través de [The Odds API](https://the-odds-api.com), calcula el margen de cada mercado, encuentra la mejor cuota entre casas de apuestas, estima el valor esperado (EV) por consenso de mercado y lleva un registro auditable.

> [!IMPORTANT]
> El proyecto **solo analiza y registra**: nunca coloca apuestas reales ni hace scraping de casas de apuestas. Un EV positivo es una estimación, no una garantía de ganancia.

---

## Tabla de contenidos

- [Características](#características)
- [Arquitectura](#arquitectura)
- [Stack tecnológico](#stack-tecnológico)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Instalación](#instalación)
- [Uso](#uso)
- [Docker y escaneo programado](#docker-y-escaneo-programado)
- [Calidad y seguridad](#calidad-y-seguridad)
- [Conceptos y fórmulas](#conceptos-y-fórmulas)
- [Autor](#autor)

---

## Características

- **Control de cuota de la API**: estima el costo de cada consulta (regiones × mercados) antes de llamarla, pide confirmación y se niega a ejecutar si quedan menos consultas que la reserva mínima (`MIN_QUOTA_RESERVE`, 50 por defecto). Caché local de 10 minutos para no gastar cuota en consultas repetidas.
- **Normalización de eventos**: convierte la respuesta de la API en filas por evento, mercado, línea y resultado, con horarios en UTC y en hora de Chile, y deja un conteo de las filas descartadas y su motivo.
- **Ranking por margen**: ordena los resultados por el margen combinado del mercado usando la mejor cuota de cada resultado entre todas las casas. Marca los posibles arbitrajes (`ARB`).
- **Valor esperado por consenso**: con al menos 4 casas, calcula probabilidades sin margen (método proporcional), usa Pinnacle como referencia *sharp* cuando está disponible y etiqueta cada resultado como `revisar`, `neutro` o `evitar`.
- **Filtros**: por deporte, mercado, horas hasta el inicio y rango de cuotas.
- **Ledger en SQLite**: 4 pozos, registro de apuestas (directo o desde una fila del último ranking), liquidación, fusión de pozos y cálculo de la cuota necesaria para llegar a la meta. Todo el dinero se guarda en enteros (CLP) y cada operación queda en un log de auditoría **inmutable** (triggers que impiden `UPDATE`/`DELETE`).
- **Reportes y exportación**: resumen con saldos, distancia a la meta, ganadas/perdidas, ROI, margen y CLV (*closing line value*); exportación a CSV o Excel (`.xlsx`).
- **Programador propio**: escaneos a horas fijas en hora de Chile (con `zoneinfo`, respeta los cambios de horario), sin depender de `cron` ni de permisos de root.

## Arquitectura

```
                 ┌──────────────┐
  The Odds API ─►│  client.py   │  control de cuota · caché 10 min · reintentos
                 └──────┬───────┘
                        ▼
                 ┌──────────────┐      ┌──────────────┐
                 │ normalize.py │─────►│   store.py   │  capturas en data/
                 └──────┬───────┘      └──────────────┘
                        ▼
        ┌───────────────┴───────────────┐
        ▼                               ▼
 ┌──────────────┐                ┌──────────────┐
 │   rank.py    │  margen, ARB   │   value.py   │  consenso, EV, etiqueta
 └──────┬───────┘                └──────┬───────┘
        └───────────────┬───────────────┘
                        ▼
                 ┌──────────────┐      ┌──────────────┐
                 │    cli.py    │─────►│  ledger.py   │  SQLite · log inmutable
                 └──────┬───────┘      └──────────────┘
                        ▼
             report.py · export.py (CSV / xlsx) · scheduler.py
```

La matemática (`math.py`, `value.py`, `rank.py`) son **funciones puras** sin I/O; la red, el disco y la base de datos viven en módulos aislados. Eso permite probar toda la lógica con fixtures JSON grabadas, sin llamar nunca a la API real desde los tests.

## Stack tecnológico

| Área | Herramientas |
|---|---|
| Lenguaje | Python 3.12 |
| CLI y presentación | Typer, Rich |
| HTTP y validación | httpx, Pydantic, pydantic-settings, python-dotenv |
| Persistencia | SQLite (stdlib), openpyxl para Excel |
| Calidad | pytest, pytest-cov, Hypothesis, respx, Ruff, mypy `--strict` |
| Seguridad | pip-audit, Bandit |
| Despliegue | Docker multi-stage, Docker Compose, GitHub Actions |

## Estructura del repositorio

```
odds-project/
├── src/odds_scanner/
│   ├── cli.py            # Comandos: sports, fetch, scan, rank, value, export, report, quota, pipeline, schedule
│   ├── ledger_cli.py     # Comandos del ledger: init, bet, settle, merge, plan, status, log
│   ├── client.py         # Cliente de The Odds API con control de cuota y caché
│   ├── normalize.py      # Respuesta de la API → filas normalizadas
│   ├── math.py           # Probabilidad implícita, margen, overround, EV (funciones puras)
│   ├── rank.py           # Ranking por margen combinado y detección de arbitraje
│   ├── value.py          # Probabilidad justa por consenso y etiquetas de EV
│   ├── ledger.py         # Pozos, apuestas y log de auditoría en SQLite
│   ├── store.py          # Capturas y último ranking en data/
│   ├── report.py         # Resumen de saldos, ROI y CLV
│   ├── export.py         # CSV y xlsx
│   ├── scheduler.py      # Corridas programadas en hora de Chile
│   ├── doctor.py         # Chequeo del entorno
│   └── config.py         # Configuración desde .env
├── tests/                # ~220 tests, sin red (fixtures + respx)
├── fixtures/             # Respuestas grabadas de la API y casos sintéticos
├── data/                 # Capturas, ledger y logs (ignorado por git)
├── Dockerfile
├── docker-compose.yml
├── Makefile
└── .github/workflows/ci.yml
```

## Instalación

**Requisitos:** Python 3.12+, `make` (en Windows, GNU Make) y una API key gratuita de [the-odds-api.com](https://the-odds-api.com).

```bash
git clone https://github.com/abrahampuente/ODDS-Project.git
cd ODDS-Project

make install            # crea .venv e instala el paquete con extras de desarrollo
cp .env.example .env    # en Windows: copy .env.example .env
```

Completa `.env`:

```dotenv
ODDS_API_KEY=tu_api_key
MIN_QUOTA_RESERVE=50
```

Verifica el entorno:

```bash
.venv/bin/odds-scanner doctor        # Linux/macOS
.venv\Scripts\odds-scanner doctor    # Windows
```

## Uso

Los ejemplos asumen el entorno virtual activado (`source .venv/bin/activate` o `.venv\Scripts\activate`).

### Consultar y rankear

```bash
odds-scanner quota --refresh                 # estado de la cuota (no consume consultas)
odds-scanner sports                          # deportes activos (gratis)
odds-scanner scan --sports soccer_epl --dry-run         # muestra el costo sin llamar a la API
odds-scanner scan --sports soccer_epl,basketball_nba    # captura y rankea (pide confirmación)

odds-scanner rank --hours 48 --odds-min 1.5 --odds-max 3   # ranking por margen desde la última captura
odds-scanner rank --view value                             # ranking por EV
odds-scanner rank --from-fixture fixtures/synthetic_arbitrage.json   # sin red, con datos grabados
```

### Ledger de pozos

```bash
odds-scanner init                                  # crea los 4 pozos de 25.000 CLP
odds-scanner bet --row 3 --pot 1 --stake 5000      # registra la fila 3 del último ranking
odds-scanner settle 1 won                          # liquida la apuesta 1 (won / lost / void)
odds-scanner plan                                  # cuota necesaria para llegar a 500.000
odds-scanner status                                # pozos y apuestas pendientes
odds-scanner report                                # saldos, ROI, margen y CLV
odds-scanner export --format xlsx                  # ranking, apuestas y pozos a Excel
odds-scanner log                                   # log de auditoría inmutable
```

Los comandos del ledger también están disponibles bajo el grupo `odds-scanner ledger …`. Usa `odds-scanner --help` o `odds-scanner <comando> --help` para ver todas las opciones.

## Docker y escaneo programado

La imagen se construye en dos etapas, corre con un usuario sin privilegios (UID 10001), con sistema de archivos de solo lectura, sin capacidades y con `no-new-privileges`. La API key **nunca** se copia a la imagen: llega en tiempo de ejecución desde `.env`.

```bash
docker compose build
docker compose run --rm scanner doctor
docker compose run --rm scanner                # corrida completa: quota → scan --yes → rank → export
docker compose --profile scheduler up -d       # escaneo automático a las 10:00 y 18:00 (hora de Chile)
```

Variables configurables: `SCAN_SPORTS`, `SCAN_MARKETS`, `SCAN_REGIONS` y `SCHEDULE_TIMES`. Si la reserva de cuota no alcanza, la corrida se registra en `data/scheduler.log` y no llama a la API.

## Calidad y seguridad

```bash
make check      # ruff (lint + formato) + mypy --strict + pytest con cobertura mínima de 85 %
make security   # pip-audit (dependencias vulnerables) + bandit (análisis estático)
```

El pipeline de GitHub Actions ejecuta `make check`, los escaneos de seguridad y una prueba de humo de la imagen Docker en cada push y pull request.

Reglas que sigue el proyecto:

- La API key vive solo en `.env`; nunca en código, logs ni commits.
- Los tests nunca llaman a la API real: usan fixtures JSON grabadas y `respx`.
- Dinero en enteros (CLP); cuotas con 2–3 decimales.

## Conceptos y fórmulas

| Concepto | Fórmula |
|---|---|
| Probabilidad implícita | `1 / cuota` |
| Overround | `Σ 1 / cuota_i` |
| Margen de la casa | `overround − 1` |
| Margen combinado | margen usando la mejor cuota de cada resultado entre todas las casas (negativo = arbitraje) |
| Probabilidad justa | `(1 / cuota_i) / overround` (método proporcional) |
| Valor esperado | `cuota × probabilidad_justa − 1` |
| Cuota objetivo | `meta / monto` |

## Autor

**Abraham Puente** — Backend & DevSecOps Developer · Ingeniería en Informática, Duoc UC
GitHub: [@abrahampuente](https://github.com/abrahampuente)

> Proyecto personal con fines de aprendizaje y análisis. Las apuestas implican riesgo de pérdida; juega con responsabilidad.
