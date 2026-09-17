# collector_orr_uk

Standalone collector for official Office of Rail and Road ODS tables 7180 (regulated/unregulated fares) and 7182 (ticket type). It preserves raw annual fare indices: 54 series, 1,348 observations, two snapshots and history from 1995 through 2026. The official data portal and downloadable ODS endpoints make this source automatable.

Current-year observations use the workbook's official timestamp. Earlier values without archived releases are `first_seen`; revised history is not retroactively exposed.

## Install and run (PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install .
Copy-Item .env.example .env
pytest -q
python main.py
```

Set `COLLECTOR_DB_URL` and allow `dataportal.orr.gov.uk`. `odfpy` is explicitly declared. Databricks is optional via `.[databricks]`. Source smoke: `python -c "from scripts.extract import collect; x=collect(); print(len(x.catalog), len(x.observations))"`.

See [METHODOLOGY.md](METHODOLOGY.md) and [POINT_IN_TIME.md](POINT_IN_TIME.md).
