from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import json
import httpx
import asyncio
import os
import logging
from datetime import datetime
from typing import List, Dict, Any
from contextlib import asynccontextmanager

# === НОВАЯ БИБЛИОТЕКА GOOGLE GEN AI ===
from google import genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Инициализация Gemini Client ===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY не найден в .env")

# Новый клиент — без configure, без лишних движений
client = genai.Client(api_key=GEMINI_API_KEY)

# === WebSocket менеджер ===
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"WebSocket disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.error(f"Broadcast error: {e}")

manager = ConnectionManager()

# === Lifespan ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    telemetry_task = asyncio.create_task(update_telemetry_background())
    logger.info("Liberty Formula started")
    yield
    telemetry_task.cancel()
    logger.info("Liberty Formula shutdown")

app = FastAPI(title="Liberty Formula API", lifespan=lifespan)

# === CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === OpenF1 API клиент ===
OPENF1_BASE = "https://api.openf1.org/v1"

async def fetch_openf1(endpoint: str, params: Dict[str, Any] = None) -> List[Dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = f"{OPENF1_BASE}/{endpoint}"
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"OpenF1 error: {e}")
        return []

async def get_current_session() -> Dict:
    data = await fetch_openf1("sessions", {"year": 2026})
    if not data:
        return {}
    sessions = sorted(data, key=lambda x: x.get("date_start", ""), reverse=True)
    return sessions[0] if sessions else {}

async def get_live_timing(session_key: str) -> Dict:
    positions = await fetch_openf1("position", {"session_key": session_key})
    intervals = await fetch_openf1("intervals", {"session_key": session_key})
    drivers = await fetch_openf1("drivers", {"session_key": session_key})
    return {"positions": positions, "intervals": intervals, "drivers": drivers}

# === Глобальные переменные ===
last_telemetry_data = {}
race_control_notices = []
telemetry_running = True

# === Фоновая задача ===
async def update_telemetry_background():
    global last_telemetry_data, race_control_notices
    while telemetry_running:
        try:
            session = await get_current_session()
            if not session:
                await asyncio.sleep(5)
                continue

            session_key = session.get("session_key")
            if not session_key:
                await asyncio.sleep(5)
                continue

            timing = await get_live_timing(str(session_key))
            if timing:
                message = {
                    "type": "telemetry",
                    "data": timing,
                    "session": {
                        "name": session.get("meeting_name", "Unknown GP"),
                        "country": session.get("country_name", ""),
                    },
                    "timestamp": datetime.now().isoformat()
                }
                last_telemetry_data = message
                await manager.broadcast(json.dumps(message))

            race_control = await fetch_openf1("race_control_messages", {"session_key": session_key})
            if race_control:
                race_control_notices = race_control[-5:]

        except Exception as e:
            logger.error(f"Telemetry background error: {e}")
        await asyncio.sleep(3)

# === AI-функция (gemini-3.6-flash) ===
async def ask_gemini(prompt: str, system: str = None) -> str:
    try:
        # Новый способ — через models.generate_content
        # Модель gemini-3.6-flash — актуальная на 2026 год [citation:1][citation:7]
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=system
            ) if system else None
        )
        return response.text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None

# === Эндпоинты ===

@app.get("/")
async def root():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/api/session")
async def get_session():
    session = await get_current_session()
    if not session:
        return {"status": "error", "message": "No active session found"}
    return {"status": "ok", "data": session}

@app.get("/api/telemetry")
async def get_telemetry():
    if not last_telemetry_data:
        return {"status": "error", "message": "No telemetry data yet"}
    return {"status": "ok", "data": last_telemetry_data}

@app.get("/api/racecontrol")
async def get_race_control():
    return {"status": "ok", "data": race_control_notices}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid JSON"}))
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)

@app.get("/api/ai/comment")
async def get_ai_comment():
    if not GEMINI_API_KEY:
        return {"status": "error", "message": "Gemini not configured"}

    if not last_telemetry_data:
        return {"status": "error", "message": "No telemetry data"}

    try:
        telemetry = last_telemetry_data.get("data", {})
        positions = telemetry.get("positions", [])

        if not positions:
            return {"status": "error", "message": "No position data"}

        top_positions = sorted(positions, key=lambda x: x.get("position", 999))[:3]
        top_text = ", ".join([f"#{p.get('driver_number')} (P{p.get('position')})" for p in top_positions])

        prompt = f"""
        You are a professional F1 commentator.
        Current top 3: {top_text}.
        Provide a short, exciting commentary (1-2 sentences) about the race situation.
        Focus on the battle for the lead.
        """

        system = "You are a professional F1 commentator. Speak with energy and excitement."

        commentary = await ask_gemini(prompt, system)
        if commentary:
            return {"status": "ok", "commentary": commentary}
        return {"status": "error", "message": "No commentary generated"}
    except Exception as e:
        logger.error(f"AI comment error: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
