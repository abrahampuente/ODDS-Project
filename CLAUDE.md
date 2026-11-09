# Proyecto: odds-scanner

Herramienta en Python 3.12 que escanea cuotas deportivas vía The Odds API,
calcula el margen de cada mercado, encuentra la mejor cuota entre casas y
lleva el registro de 4 pozos de 25.000 CLP con meta total de 500.000 CLP.
NUNCA coloca apuestas reales ni hace scraping de casas de apuestas.

## Stack
- Python 3.12, uv o pip + venv, pytest, ruff, mypy (strict), httpx, pydantic,
  sqlite3 (stdlib), typer (CLI), rich (tablas), python-dotenv.
- Estructura: src/odds_scanner/, tests/, fixtures/, data/ (git-ignored).

## Reglas de validación (obligatorias)
1. Trabaja en fases. Una fase = un objetivo con criterios de aceptación.
2. Antes de implementar, escribe los tests que prueban los criterios.
3. Al terminar, ejecuta `make check` (ruff + mypy + pytest) y la prueba de
   humo de la fase. Pega la SALIDA REAL de la terminal, sin resumirla.
4. Prohibido declarar algo "listo" o "funcionando" sin salida de comandos
   que lo demuestre.
5. Si un comando falla: diagnostica, corrige, re-ejecuta. Máximo 3 intentos;
   si sigue fallando, DETENTE y reporta el error, lo que probaste y tu hipótesis.
6. Prohibido borrar, saltar (skip/xfail) o debilitar tests para que pasen.
7. Cobertura mínima 85% en src/odds_scanner (pytest --cov --cov-fail-under=85).
8. Commit solo con todo en verde: `git commit -m "faseN: <resumen>"`.
9. Termina cada fase con un bloque "REPORTE DE FASE":
   - criterios de aceptación y estado de cada uno (OK/FALLA)
   - comandos ejecutados y su resultado
   - consumo de cuota de API en esta fase
   - riesgos o deudas pendientes

## Reglas de API
- La API key vive solo en .env (ODDS_API_KEY). Nunca en código, logs ni commits.
- Tests NUNCA llaman a la API real: usan fixtures JSON grabadas.
- Toda llamada real pasa por el cliente con control de cuota; si quedan
  menos de 50 consultas, se niega a ejecutar y avisa.

## Estilo
- Funciones puras para la matemática; I/O aislado en módulos propios.
- Tipado completo, docstrings breves en español, nombres en inglés.
- Dinero en enteros (CLP), cuotas en Decimal o float con 2-3 decimales.