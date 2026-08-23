from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .api import router
from .auto_director_worker import AutoDirectorWorker

app = FastAPI(title="AI Narrative World Engine API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:4300", "http://127.0.0.1:4300"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
auto_director_worker = AutoDirectorWorker()

@app.on_event("startup")
def start_local_workers() -> None:
    auto_director_worker.start()


@app.on_event("shutdown")
def stop_local_workers() -> None:
    auto_director_worker.stop()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
