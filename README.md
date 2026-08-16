# AI Narrative World Engine

## Start API

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"
$env:DATABASE_URL = "postgresql+psycopg://narrative:narrative@localhost:5432/narrative"
alembic -c apps/api/alembic.ini upgrade head
uvicorn app.main:app --app-dir apps/api --reload
```

## Start database

```powershell
docker compose up -d postgres
```

## Tests

```powershell
pytest
```
