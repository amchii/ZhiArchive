from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from archive.api.endpoints import auth, logs, zhi_login, zhi_monitor

app = FastAPI(title="Zhi Archive")
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(logs.router, prefix="/log", tags=["log"])
app.include_router(zhi_login.router, prefix="/zhi/login", tags=["zhi_login"])
app.include_router(zhi_monitor.router, prefix="/zhi/monitor", tags=["zhi_monitor"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def index():
    return {"message": "Hello world"}
