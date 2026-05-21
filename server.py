"""
AsistIA - Backend de Reconocimiento Facial
==========================================
Requiere Python 3.9+

Instalación:
    pip install fastapi uvicorn websockets insightface opencv-python numpy pillow

Ejecutar:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload

Acceder al kiosko:
    Abrir kiosk.html en el navegador de la tableta
    (debe estar en la misma red que el servidor)
"""

import asyncio
import base64
import csv
import hashlib
import os
import io
import json
import logging
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

PANAMA_TZ = ZoneInfo("America/Panama")

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from PIL import Image

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("asistia")

# ── Globals ───────────────────────────────────────────────────────────────────
DB_PATH    = os.environ.get("DB_PATH",     "asistia.db")
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "models"))
RECOGNITION_THRESHOLD = float(os.environ.get("RECOGNITION_THRESHOLD", "0.45"))
LATE_HOUR   = int(os.environ.get("LATE_HOUR",   "7"))
LATE_MINUTE = int(os.environ.get("LATE_MINUTE", "15"))

ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
SESSION_HOURS  = int(os.environ.get("SESSION_HOURS", "8"))

if not ADMIN_EMAIL or not ADMIN_PASSWORD:
    raise RuntimeError(
        "Variables de entorno requeridas: ADMIN_EMAIL y ADMIN_PASSWORD. "
        "Agrégalas antes de arrancar el servidor."
    )

face_app = None                      # InsightFace app (se carga al iniciar)
student_cache: dict = {}             # Cache de embeddings: {id: [embedding, ...]}


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global face_app
    log.info("Iniciando AsistIA...")
    init_db()
    log.info("Base de datos inicializada ✓")
    
    try:
        import insightface
        face_app = insightface.app.FaceAnalysis(
            name="buffalo_sc",          # Modelo rápido y liviano (~30MB)
            root=str(MODELS_DIR),
            providers=["CPUExecutionProvider"]
        )
        face_app.prepare(ctx_id=0, det_size=(320, 320))
        log.info("InsightFace cargado ✓")
    except Exception as e:
        log.error(f"Error cargando InsightFace: {e}")
        log.warning("Corriendo en modo DEMO (sin reconocimiento real)")

    load_student_cache()
    log.info(f"Cache cargado: {len(student_cache)} estudiantes ✓")
    log.info("AsistIA listo en http://0.0.0.0:8000")
    
    yield
    
    log.info("Apagando AsistIA...")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="AsistIA", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir kiosk.html en la raíz con no-cache para evitar versiones desactualizadas
@app.get("/", response_class=HTMLResponse)
@app.get("/kiosk.html", response_class=HTMLResponse)
async def serve_kiosk():
    path = Path("static/kiosk.html")
    if not path.exists():
        return HTMLResponse("<h1>kiosk.html no encontrado</h1>", status_code=404)
    content = path.read_text(encoding="utf-8")
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache"
    })

# Archivos estáticos (assets: logos, favicon)
if Path("static").exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS students (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            cedula      TEXT DEFAULT '',
            nivel       TEXT DEFAULT '',
            grade       TEXT NOT NULL,
            has_biometric INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS face_embeddings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id  TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            embedding   BLOB NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id  TEXT NOT NULL REFERENCES students(id),
            student_name TEXT NOT NULL,
            grade       TEXT NOT NULL,
            date        TEXT NOT NULL,
            time        TEXT NOT NULL,
            status      TEXT NOT NULL,
            confidence  REAL,
            UNIQUE(student_id, date)
        );

        CREATE TABLE IF NOT EXISTS app_users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT UNIQUE NOT NULL,
            name       TEXT NOT NULL,
            role       TEXT NOT NULL DEFAULT 'operator',
            password_hash TEXT NOT NULL,
            active     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS app_sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL
        );
    """)
    # Migración: columnas legacy
    for col, default in [("cedula","''"), ("nivel","''"), ("has_biometric","0")]:
        try:
            conn.execute(f"ALTER TABLE students ADD COLUMN {col} TEXT DEFAULT {default}")
            conn.commit()
        except Exception:
            pass

    # Crear admin por defecto solo si no existe
    existing = conn.execute("SELECT id FROM app_users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO app_users (email, name, role, password_hash, active, created_at) VALUES (?,?,?,?,1,?)",
            (ADMIN_EMAIL, "Administrador", "admin", _hash_password(ADMIN_PASSWORD), now_panama().isoformat())
        )
        conn.commit()
        log.info(f"Admin creado: {ADMIN_EMAIL}")

    conn.close()


# ── Auth helpers ──────────────────────────────────────────────────────────────
def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}:{h.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        h2 = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return secrets.compare_digest(h, h2.hex())
    except Exception:
        return False


def _create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(40)
    expires = (now_panama() + timedelta(hours=SESSION_HOURS)).isoformat()
    conn = get_db()
    # Limpiar sesiones expiradas del mismo usuario
    conn.execute("DELETE FROM app_sessions WHERE user_id=? OR expires_at<?",
                 (user_id, now_panama().isoformat()))
    conn.execute("INSERT INTO app_sessions (token, user_id, expires_at) VALUES (?,?,?)",
                 (token, user_id, expires))
    conn.commit()
    conn.close()
    return token


def _get_session_user(token: str) -> Optional[dict]:
    if not token:
        return None
    conn = get_db()
    row = conn.execute("""
        SELECT u.id, u.email, u.name, u.role, u.active
        FROM app_users u
        JOIN app_sessions s ON s.user_id = u.id
        WHERE s.token = ? AND s.expires_at > ? AND u.active = 1
    """, (token, now_panama().isoformat())).fetchone()
    conn.close()
    return dict(row) if row else None


def require_auth(request: Request) -> dict:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    user = _get_session_user(token)
    if not user:
        raise HTTPException(401, "No autenticado")
    return user


def require_admin(request: Request) -> dict:
    user = require_auth(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Solo administradores")
    return user


def load_student_cache():
    """Carga todos los embeddings en memoria para comparación rápida."""
    global student_cache
    student_cache = {}
    conn = get_db()
    rows = conn.execute("""
        SELECT fe.student_id, fe.embedding
        FROM face_embeddings fe
        JOIN students s ON s.id = fe.student_id
    """).fetchall()
    conn.close()

    for row in rows:
        sid = row["student_id"]
        embedding = np.frombuffer(row["embedding"], dtype=np.float32)
        if sid not in student_cache:
            student_cache[sid] = []
        student_cache[sid].append(embedding)

    log.info(f"Cache actualizado: {len(student_cache)} estudiantes, "
             f"{sum(len(v) for v in student_cache.values())} embeddings totales")


# ── Face utilities ────────────────────────────────────────────────────────────
def decode_frame(b64_data: str) -> Optional[np.ndarray]:
    """Decodifica un frame base64 → numpy array BGR."""
    try:
        if "," in b64_data:
            b64_data = b64_data.split(",", 1)[1]
        raw = base64.b64decode(b64_data)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    except Exception as e:
        log.warning(f"Error decodificando frame: {e}")
        return None


def get_faces(frame: np.ndarray):
    """Detecta rostros en un frame usando InsightFace."""
    if face_app is None:
        return []
    try:
        return face_app.get(frame)
    except Exception as e:
        log.warning(f"Error en detección: {e}")
        return []


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Distancia coseno entre dos embeddings (0 = idéntico, 2 = opuesto)."""
    a_norm = a / (np.linalg.norm(a) + 1e-6)
    b_norm = b / (np.linalg.norm(b) + 1e-6)
    return float(1 - np.dot(a_norm, b_norm))


def find_match(query_embedding: np.ndarray) -> tuple[Optional[str], float]:
    """Busca el estudiante más parecido en el cache."""
    best_id = None
    best_dist = float("inf")

    for student_id, embeddings in student_cache.items():
        # Promedia la distancia contra todas las fotos del estudiante
        distances = [cosine_distance(query_embedding, e) for e in embeddings]
        avg_dist = sum(distances) / len(distances)
        if avg_dist < best_dist:
            best_dist = avg_dist
            best_id = student_id

    if best_dist <= RECOGNITION_THRESHOLD:
        confidence = round((1 - best_dist / RECOGNITION_THRESHOLD) * 100, 1)
        return best_id, confidence
    return None, 0.0


def now_panama() -> datetime:
    return datetime.now(PANAMA_TZ)

def today_panama() -> str:
    return now_panama().date().isoformat()

def determine_status() -> str:
    now = now_panama()
    if now.hour > LATE_HOUR or (now.hour == LATE_HOUR and now.minute >= LATE_MINUTE):
        return "late"
    return "present"


# ── WebSocket: Kiosko (reconocimiento) ───────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws) if hasattr(self.active, "discard") else None
        if ws in self.active:
            self.active.remove(ws)

kiosk_manager = ConnectionManager()
register_manager = ConnectionManager()

# Evitar marcar el mismo rostro múltiples veces en poco tiempo
recent_marks: dict[str, float] = {}   # {student_id: timestamp}
COOLDOWN_SECONDS = 8


@app.websocket("/ws/kiosk")
async def ws_kiosk(ws: WebSocket, token: Optional[str] = None):
    user = _get_session_user(token or "")
    if not user:
        await ws.close(code=4401)
        return
    await kiosk_manager.connect(ws)
    log.info("Kiosko conectado")
    
    try:
        while True:
            data = await ws.receive_json()
            frame = decode_frame(data.get("frame", ""))
            
            if frame is None:
                await ws.send_json({"type": "error", "msg": "Frame inválido"})
                continue

            faces = get_faces(frame)

            if not faces:
                await ws.send_json({"type": "no_face"})
                continue

            # Usar el rostro más grande (más cercano a la cámara)
            face = max(faces, key=lambda f: (f.bbox[2]-f.bbox[0]) * (f.bbox[3]-f.bbox[1]))
            embedding = face.embedding

            if embedding is None:
                await ws.send_json({"type": "no_face"})
                continue

            student_id, confidence = find_match(embedding)

            if student_id is None:
                await ws.send_json({"type": "unknown", "confidence": 0})
                continue

            # Cooldown — evitar marcar en loop
            now = time.time()
            if student_id in recent_marks and now - recent_marks[student_id] < COOLDOWN_SECONDS:
                await ws.send_json({"type": "already_marked", "student_id": student_id})
                continue

            recent_marks[student_id] = now

            # Obtener datos del estudiante
            conn = get_db()
            student = conn.execute(
                "SELECT * FROM students WHERE id = ?", (student_id,)
            ).fetchone()
            conn.close()

            if not student:
                await ws.send_json({"type": "unknown"})
                continue

            # Registrar asistencia
            today = today_panama()
            time_str = now_panama().strftime("%H:%M")
            status = determine_status()

            conn = get_db()
            try:
                conn.execute("""
                    INSERT INTO attendance (student_id, student_name, grade, date, time, status, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (student_id, student["name"], student["grade"], today, time_str, status, confidence))
                conn.commit()
                log.info(f"✓ Asistencia: {student['name']} ({confidence}%) - {status}")
            except sqlite3.IntegrityError:
                # Ya marcado hoy (UNIQUE constraint)
                status = "already_today"
                log.info(f"→ Ya marcado: {student['name']}")
            finally:
                conn.close()

            await ws.send_json({
                "type": "match",
                "student": {
                    "id": student_id,
                    "name": student["name"],
                    "grade": student["grade"],
                },
                "confidence": confidence,
                "status": status,
                "time": time_str,
            })

    except WebSocketDisconnect:
        kiosk_manager.disconnect(ws)
        log.info("Kiosko desconectado")
    except Exception as e:
        log.error(f"Error en kiosko WS: {e}")
        kiosk_manager.disconnect(ws)


# ── Pose helpers ─────────────────────────────────────────────────────────────
# Umbrales de yaw normalizado por fase.
# Estimamos yaw desde los 5 kps del detector:
#   kps[0]=ojo_izq  kps[1]=ojo_der  kps[2]=nariz
# Asimetría = (dist_nariz_a_ojo_izq - dist_nariz_a_ojo_der) / ancho_entre_ojos
#   ~0   → frontal
#   > 0  → cara girada a la izquierda (ojo der más cerca de nariz)
#   < 0  → cara girada a la derecha
POSE_THRESHOLDS = {
    0: (-0.25, 0.25),   # frente
    1: ( 0.30, 0.90),   # izquierda
    2: (-0.90, -0.30),  # derecha
}

def estimate_yaw(face) -> Optional[float]:
    """Yaw normalizado desde keypoints. Retorna None si no hay kps."""
    kps = face.kps if face.kps is not None else None
    if kps is None or len(kps) < 3:
        # Fallback: intentar desde face.pose si existe
        if face.pose is not None:
            return float(face.pose[1]) / 60.0  # normalizar ~±60° → ±1
        return None
    eye_l = np.array(kps[0])
    eye_r = np.array(kps[1])
    nose  = np.array(kps[2])
    eye_width = float(np.linalg.norm(eye_r - eye_l))
    if eye_width < 1:
        return None
    dist_l = float(nose[0] - eye_l[0])   # positivo = nariz a la derecha del ojo izq
    dist_r = float(eye_r[0] - nose[0])   # positivo = ojo der a la derecha de nariz
    # Asimetría normalizada: positivo → girado a la izquierda
    return (dist_l - dist_r) / eye_width


def check_pose(face, phase: int) -> tuple[bool, float, str]:
    """Devuelve (pose_ok, yaw_norm, mensaje_guia)."""
    yaw = estimate_yaw(face)
    if yaw is None:
        return True, 0.0, ""   # sin kps → no bloquear

    lo, hi = POSE_THRESHOLDS[phase]
    if lo <= yaw <= hi:
        return True, yaw, ""

    if phase == 0:
        msg = "Mira directo a la cámara"
    elif phase == 1:
        msg = "Gira más a tu izquierda" if yaw < lo else "Baja el giro — casi de frente"
    else:
        msg = "Gira más a tu derecha" if yaw > hi else "Baja el giro — casi de frente"

    return False, yaw, msg


# ── WebSocket: Registro automático ───────────────────────────────────────────
@app.websocket("/ws/register")
async def ws_register(ws: WebSocket, token: Optional[str] = None):
    user = _get_session_user(token or "")
    if not user:
        await ws.close(code=4401)
        return
    await register_manager.connect(ws)
    log.info("Registro conectado")

    try:
        while True:
            data = await ws.receive_json()
            frame = decode_frame(data.get("frame", ""))
            phase = data.get("phase", 0)    # 0=frente, 1=izq, 2=der

            if frame is None:
                await ws.send_json({"type": "error"})
                continue

            faces = get_faces(frame)

            if not faces:
                await ws.send_json({"type": "no_face", "phase": phase, "yaw": 0})
                continue

            face = max(faces, key=lambda f: (f.bbox[2]-f.bbox[0]) * (f.bbox[3]-f.bbox[1]))

            # Calidad
            bbox = face.bbox
            face_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            frame_area = frame.shape[0] * frame.shape[1]
            size_ratio = face_area / frame_area
            det_score = float(face.det_score) if hasattr(face, "det_score") else 0.9
            quality = min(100, round(size_ratio * 400 + det_score * 40, 1))

            if quality < 30:
                await ws.send_json({
                    "type": "poor_quality",
                    "phase": phase,
                    "quality": quality,
                    "yaw": 0,
                    "msg": "Acércate más a la cámara"
                })
                continue

            # Pose
            pose_ok, yaw, guide = check_pose(face, phase)
            embedding = face.embedding.tolist() if face.embedding is not None else None

            if not pose_ok:
                await ws.send_json({
                    "type": "wrong_pose",
                    "phase": phase,
                    "quality": quality,
                    "yaw": round(yaw, 1),
                    "pose_ok": False,
                    "guide": guide,
                })
                continue

            await ws.send_json({
                "type": "detected",
                "phase": phase,
                "quality": quality,
                "yaw": round(yaw, 1),
                "pose_ok": True,
                "guide": "",
                "embedding": embedding,
                "bbox": [float(x) for x in face.bbox],
            })

    except WebSocketDisconnect:
        register_manager.disconnect(ws)
        log.info("Registro desconectado")
    except Exception as e:
        log.error(f"Error en registro WS: {e}")
        register_manager.disconnect(ws)


# ── REST API ──────────────────────────────────────────────────────────────────

# Auth endpoints (públicos) ───────────────────────────────────────────────────

@app.post("/api/auth/login")
async def login(body: dict):
    email    = body.get("email", "").strip().lower()
    password = body.get("password", "")
    if not email or not password:
        raise HTTPException(400, "Email y contraseña requeridos")

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM app_users WHERE email=? AND active=1", (email,)
    ).fetchone()
    conn.close()

    if not user or not _verify_password(password, user["password_hash"]):
        raise HTTPException(401, "Credenciales incorrectas")

    token = _create_session(user["id"])
    log.info(f"Login: {email} ({user['role']})")
    return {"token": token, "name": user["name"], "email": user["email"], "role": user["role"]}


@app.post("/api/auth/logout")
async def logout(request: Request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if token:
        conn = get_db()
        conn.execute("DELETE FROM app_sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: dict = Depends(require_auth)):
    return {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]}


# User management (admin only) ────────────────────────────────────────────────

@app.get("/api/users")
async def list_users(user: dict = Depends(require_admin)):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, email, name, role, active, created_at FROM app_users ORDER BY role DESC, name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/users")
async def create_user(body: dict, user: dict = Depends(require_admin)):
    email    = body.get("email", "").strip().lower()
    name     = body.get("name", "").strip()
    password = body.get("password", "").strip()
    role     = body.get("role", "operator")

    if not email or not name or not password:
        raise HTTPException(400, "Email, nombre y contraseña son requeridos")
    if role not in ("admin", "operator"):
        raise HTTPException(400, "Rol inválido")
    if len(password) < 6:
        raise HTTPException(400, "La contraseña debe tener al menos 6 caracteres")

    conn = get_db()
    if conn.execute("SELECT id FROM app_users WHERE email=?", (email,)).fetchone():
        conn.close()
        raise HTTPException(409, "El email ya está registrado")

    conn.execute(
        "INSERT INTO app_users (email, name, role, password_hash, active, created_at) VALUES (?,?,?,?,1,?)",
        (email, name, role, _hash_password(password), now_panama().isoformat())
    )
    conn.commit()
    conn.close()
    log.info(f"Usuario creado: {email} ({role}) por {user['email']}")
    return {"ok": True}


@app.patch("/api/users/{uid}")
async def update_user(uid: int, body: dict, admin: dict = Depends(require_admin)):
    conn = get_db()
    target = conn.execute("SELECT * FROM app_users WHERE id=?", (uid,)).fetchone()
    if not target:
        conn.close()
        raise HTTPException(404, "Usuario no encontrado")

    # No puede desactivarse a sí mismo ni cambiar su propio rol
    if target["id"] == admin["id"] and ("active" in body or "role" in body):
        conn.close()
        raise HTTPException(400, "No puedes modificar tu propio rol o estado")

    fields, vals = [], []
    if "name" in body:
        fields.append("name=?"); vals.append(body["name"].strip())
    if "role" in body and body["role"] in ("admin","operator"):
        fields.append("role=?"); vals.append(body["role"])
    if "active" in body:
        fields.append("active=?"); vals.append(1 if body["active"] else 0)
    if "password" in body and body["password"]:
        if len(body["password"]) < 6:
            conn.close()
            raise HTTPException(400, "La contraseña debe tener al menos 6 caracteres")
        fields.append("password_hash=?"); vals.append(_hash_password(body["password"]))

    if fields:
        vals.append(uid)
        conn.execute(f"UPDATE app_users SET {','.join(fields)} WHERE id=?", vals)
        conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/users/{uid}")
async def delete_user(uid: int, admin: dict = Depends(require_admin)):
    if uid == admin["id"]:
        raise HTTPException(400, "No puedes eliminar tu propia cuenta")
    conn = get_db()
    conn.execute("DELETE FROM app_users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# Students (auth required) ────────────────────────────────────────────────────

@app.post("/api/students")
async def create_student(body: dict, _: dict = Depends(require_auth)):
    """Crea alumno con biométrica (embeddings requeridos)."""
    name  = body.get("name", "").strip()
    grade = body.get("grade", "").strip()
    cedula = body.get("cedula", "").strip()
    nivel  = body.get("nivel", "").strip()
    embeddings = body.get("embeddings", [])

    if not name or not grade:
        raise HTTPException(400, "Nombre y grado son requeridos")
    if not embeddings:
        raise HTTPException(400, "Se necesita al menos 1 embedding facial")

    student_id = f"s_{int(time.time() * 1000)}"
    now = now_panama().isoformat()

    conn = get_db()
    conn.execute(
        "INSERT INTO students (id, name, cedula, nivel, grade, has_biometric, created_at) VALUES (?,?,?,?,?,1,?)",
        (student_id, name, cedula, nivel, grade, now)
    )
    for emb in embeddings:
        arr = np.array(emb, dtype=np.float32)
        conn.execute(
            "INSERT INTO face_embeddings (student_id, embedding, created_at) VALUES (?,?,?)",
            (student_id, arr.tobytes(), now)
        )
    conn.commit()
    conn.close()
    load_student_cache()
    log.info(f"Alumno registrado: {name} ({len(embeddings)} fotos)")
    return {"id": student_id, "name": name, "grade": grade}


@app.get("/api/students")
async def list_students(q: Optional[str] = None, _: dict = Depends(require_auth)):
    conn = get_db()
    if q:
        pattern = f"%{q}%"
        rows = conn.execute("""
            SELECT s.*, COUNT(fe.id) as photo_count
            FROM students s
            LEFT JOIN face_embeddings fe ON fe.student_id = s.id
            WHERE s.name LIKE ? OR s.cedula LIKE ? OR s.nivel LIKE ?
            GROUP BY s.id ORDER BY s.name LIMIT 50
        """, (pattern, pattern, pattern)).fetchall()
    else:
        rows = conn.execute("""
            SELECT s.*, COUNT(fe.id) as photo_count
            FROM students s
            LEFT JOIN face_embeddings fe ON fe.student_id = s.id
            GROUP BY s.id ORDER BY s.name
        """).fetchall()
    conn.close()
    return [dict(s) for s in rows]


@app.patch("/api/students/{student_id}/biometric")
async def add_biometric(student_id: str, body: dict, _: dict = Depends(require_auth)):
    """Añade embeddings biométricos a un alumno ya existente (importado por CSV)."""
    embeddings = body.get("embeddings", [])
    if not embeddings:
        raise HTTPException(400, "Se necesita al menos 1 embedding")

    conn = get_db()
    student = conn.execute("SELECT id FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close()
        raise HTTPException(404, "Alumno no encontrado")

    now = now_panama().isoformat()
    # Eliminar embeddings anteriores y reemplazar
    conn.execute("DELETE FROM face_embeddings WHERE student_id=?", (student_id,))
    for emb in embeddings:
        arr = np.array(emb, dtype=np.float32)
        conn.execute(
            "INSERT INTO face_embeddings (student_id, embedding, created_at) VALUES (?,?,?)",
            (student_id, arr.tobytes(), now)
        )
    conn.execute("UPDATE students SET has_biometric=1 WHERE id=?", (student_id,))
    conn.commit()
    conn.close()
    load_student_cache()
    log.info(f"Biométrica actualizada: {student_id} ({len(embeddings)} fotos)")
    return {"ok": True}


@app.post("/api/students/import")
async def import_csv(body: dict, _: dict = Depends(require_auth)):
    """
    Importa alumnos desde CSV parseado en el frontend.
    body: { "rows": [{"name":"..","cedula":"..","nivel":"..","grade":".."}] }
    Ignora duplicados por cédula.
    """
    rows = body.get("rows", [])
    if not rows:
        raise HTTPException(400, "Sin filas")

    now = now_panama().isoformat()
    conn = get_db()
    imported, skipped = 0, 0

    for row in rows:
        name   = str(row.get("name","")).strip()
        cedula = str(row.get("cedula","")).strip()
        nivel  = str(row.get("nivel","")).strip()
        grade  = str(row.get("grade", nivel)).strip() or "Sin grado"
        if not name:
            skipped += 1
            continue
        # Verificar duplicado por cédula
        if cedula:
            exists = conn.execute("SELECT id FROM students WHERE cedula=?", (cedula,)).fetchone()
            if exists:
                skipped += 1
                continue
        student_id = f"s_{int(time.time()*1000)}_{imported}"
        conn.execute(
            "INSERT INTO students (id,name,cedula,nivel,grade,has_biometric,created_at) VALUES (?,?,?,?,?,0,?)",
            (student_id, name, cedula, nivel, grade, now)
        )
        imported += 1

    conn.commit()
    conn.close()
    load_student_cache()
    log.info(f"CSV importado: {imported} alumnos, {skipped} omitidos")
    return {"imported": imported, "skipped": skipped}


@app.delete("/api/students/{student_id}")
async def delete_student(student_id: str, _: dict = Depends(require_auth)):
    conn = get_db()
    conn.execute("DELETE FROM students WHERE id=?", (student_id,))
    conn.commit()
    conn.close()
    load_student_cache()
    return {"ok": True}


@app.get("/api/stats/dashboard")
async def dashboard_stats(days: int = 7, _: dict = Depends(require_auth)):
    """Datos para gráficas del dashboard: últimos N días."""
    conn = get_db()
    today = now_panama().date()

    # Serie de días
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days-1, -1, -1)]

    # Asistencia por día
    daily = []
    for d in dates:
        row = conn.execute("""
            SELECT
                SUM(CASE WHEN status='present' THEN 1 ELSE 0 END) as present,
                SUM(CASE WHEN status='late'    THEN 1 ELSE 0 END) as late,
                COUNT(*) as total
            FROM attendance WHERE date=?
        """, (d,)).fetchone()
        daily.append({
            "date": d,
            "present": row["present"] or 0,
            "late":    row["late"]    or 0,
            "total":   row["total"]   or 0,
        })

    # Totales de hoy
    today_str = today.isoformat()
    today_row = conn.execute("""
        SELECT
            SUM(CASE WHEN status='present' THEN 1 ELSE 0 END) as present,
            SUM(CASE WHEN status='late'    THEN 1 ELSE 0 END) as late,
            COUNT(*) as marked
        FROM attendance WHERE date=?
    """, (today_str,)).fetchone()
    total_students = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    bio_students   = conn.execute("SELECT COUNT(*) FROM students WHERE has_biometric=1").fetchone()[0]

    # Top 5 alumnos más tardíos (últimos 30 días)
    late_30 = (today - timedelta(days=30)).isoformat()
    top_late = conn.execute("""
        SELECT student_name, COUNT(*) as late_count
        FROM attendance
        WHERE status='late' AND date >= ?
        GROUP BY student_id ORDER BY late_count DESC LIMIT 5
    """, (late_30,)).fetchall()

    # Distribución por nivel (total alumnos)
    by_nivel = conn.execute("""
        SELECT nivel, COUNT(*) as cnt FROM students WHERE nivel != '' GROUP BY nivel ORDER BY cnt DESC
    """).fetchall()

    # Asistencia hoy por nivel
    nivel_att = conn.execute("""
        SELECT s.nivel,
            SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END) as present,
            SUM(CASE WHEN a.status='late'    THEN 1 ELSE 0 END) as late,
            COUNT(s.id) as total
        FROM students s
        LEFT JOIN attendance a ON a.student_id = s.id AND a.date = ?
        WHERE s.nivel != ''
        GROUP BY s.nivel ORDER BY s.nivel
    """, (today_str,)).fetchall()

    conn.close()
    return {
        "daily":          daily,
        "today_present":  today_row["present"] or 0,
        "today_late":     today_row["late"]    or 0,
        "today_absent":   total_students - (today_row["marked"] or 0),
        "total_students": total_students,
        "bio_students":   bio_students,
        "top_late":       [dict(r) for r in top_late],
        "by_nivel":       [dict(r) for r in by_nivel],
        "nivel_att":      [dict(r) for r in nivel_att],
    }


@app.get("/api/attendance")
async def get_attendance(fecha: Optional[str] = None, _: dict = Depends(require_auth)):
    today = fecha or today_panama()
    conn = get_db()
    rows = conn.execute("""
        SELECT a.*, s.nivel, s.cedula
        FROM attendance a
        LEFT JOIN students s ON s.id = a.student_id
        WHERE a.date = ? ORDER BY a.time
    """, (today,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/attendance/stats")
async def get_stats(fecha: Optional[str] = None, _: dict = Depends(require_auth)):
    today = fecha or today_panama()
    conn = get_db()
    row = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status='present' THEN 1 ELSE 0 END) as present,
            SUM(CASE WHEN status='late'    THEN 1 ELSE 0 END) as late
        FROM attendance WHERE date = ?
    """, (today,)).fetchone()
    total_students = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    conn.close()
    return {
        "date": today,
        "total_marked": row["total"],
        "present": row["present"] or 0,
        "late": row["late"] or 0,
        "absent": total_students - (row["total"] or 0),
        "total_students": total_students,
    }


@app.get("/api/attendance/export")
async def export_csv(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    _: dict = Depends(require_auth)
):
    today = today_panama()
    d_desde = desde or today
    d_hasta = hasta or today

    conn = get_db()
    rows = conn.execute(
        "SELECT date, student_name, grade, time, status, confidence FROM attendance "
        "WHERE date BETWEEN ? AND ? ORDER BY date, time",
        (d_desde, d_hasta)
    ).fetchall()
    conn.close()

    lines = ["Fecha,Nombre,Grado,Hora,Estado,Confianza"]
    for r in rows:
        lines.append(f"{r['date']},{r['student_name']},{r['grade']},{r['time']},{r['status']},{r['confidence']}%")

    from fastapi.responses import Response
    filename = f"asistencia_{d_desde}" if d_desde == d_hasta else f"asistencia_{d_desde}_al_{d_hasta}"
    return Response(
        content="\n".join(lines),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"}
    )


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "models_loaded": face_app is not None,
        "students_cached": len(student_cache),
        "time": now_panama().isoformat(),
    }
