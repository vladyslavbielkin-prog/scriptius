import os
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from audio_ws import router as audio_ws_router

app = FastAPI(title="Scriptius STT Server")
app.include_router(audio_ws_router)

@app.get("/health")
def health():
    return {"status": "ok"}

# Static files — AFTER routers so /audio WebSocket takes priority
app.mount("/", StaticFiles(directory="public", html=True), name="static")
