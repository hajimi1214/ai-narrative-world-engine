from fastapi import FastAPI
from .api import router

app = FastAPI(title="AI Narrative World Engine API", version="0.1.0")
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
