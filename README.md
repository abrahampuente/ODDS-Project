# odds_scanner

Escáner de cuotas deportivas (The Odds API).

```bash
make install        # crea .venv e instala el paquete con extras de desarrollo
cp .env.example .env  # y rellena ODDS_API_KEY
.venv/Scripts/odds-scanner doctor   # Windows (en Linux/macOS: .venv/bin/odds-scanner)
make check          # lint + typecheck + tests
```

## Cómo calcula

- **Exchanges**: Betfair, Matchbook y Smarkets cobran comisión sobre la ganancia. El
  ranking usa la cuota efectiva `1 + (bruta - 1) * (1 - comisión)` y marca la casa con
  `*`; la cuota que verás en el sitio queda en `cuota_publicada`. Por defecto Betfair 5%,
  Matchbook y Smarkets 2%; ajusta con `EXCHANGE_COMMISSIONS` en `.env`.
- **Probabilidad justa**: el margen de cada casa se quita con el método *power*, que
  corrige el sesgo favorito-longshot (las casas cargan más margen a las cuotas altas).
  El método proporcional sobrestimaba la probabilidad de los no favoritos y por eso
  inflaba el EV justo en cuotas 4-6.
- **Programador**: `docker compose --profile scheduler up -d` corre a las 10:00 y 18:00
  (Chile). Gasta hasta 1 consulta por deporte en cada corrida y se detiene al llegar a
  `SCHEDULE_MONTHLY_BUDGET` (60 por defecto) o a la reserva `MIN_QUOTA_RESERVE`
  (mínimo 50: el entorno puede subirla, no bajarla).

## Probar sin gastar cuota

```bash
odds-scanner pipeline --from-fixture fixtures/soccer_epl_2026-11-09.json  # scan, rank, export
odds-scanner scan --from-fixture fixtures/soccer_epl_2026-11-09.json      # solo scan + ranking
```

Ninguno llama a la API ni lee la cuota: la fixture se guarda como captura y el resto
del flujo (`rank`, `bet --row`, `export`, `report`) funciona igual que con datos reales.

## Reglas del ledger

- 4 pozos de 25.000 y sin recargas. `init --reset --confirm` solo funciona mientras no
  haya apuestas registradas; con apuestas, reiniciar sería una recarga y se rechaza.
- Cuota mínima 1.01; `--odds` manual con 2 o 3 decimales, tal como la muestra la casa.
- Liquidar, apostar y juntar pozos son operaciones atómicas aun con dos procesos a la
  vez: una apuesta no se puede pagar dos veces ni dejar un pozo en negativo.

## Dónde dejar el proyecto

No lo tengas dentro de OneDrive: la sincronización bloquea `.git`, `.venv` y el SQLite
del ledger. Muévelo a una carpeta local (p. ej. `C:\Users\<tú>\Projects\odds-scanner`)
y vuelve a correr `make install`, porque el venv guarda rutas absolutas.
