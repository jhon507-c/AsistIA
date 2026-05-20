# AsistIA — Sistema de Control de Asistencia Facial

Sistema de reconocimiento facial para control de asistencia escolar del **Instituto Panamericano de David**. Corre en servidor local, opera desde tabletas como kioscos y no requiere conexión a internet después de la instalación.

---

## Arquitectura

```
Tableta (navegador)
    │
    ├── WS /ws/kiosk      → Reconocimiento en tiempo real
    ├── WS /ws/register   → Captura de fotos para registro
    └── REST /api/        → CRUD, CSV import, estadísticas
         │
    FastAPI + InsightFace (buffalo_sc)
         │
    SQLite (asistia.db)
```

**Stack:**
- **Backend:** Python 3.10 · FastAPI · InsightFace `buffalo_sc` · SQLite
- **Frontend:** HTML/CSS/JS vanilla · Chart.js 4.4
- **Modelo facial:** `buffalo_sc` — detección + reconocimiento, ~30MB, corre en CPU
- **Hardware destino:** HP ProLiant ML110 Gen10 · Ubuntu Server 20.04/22.04

---

## Instalación en Ubuntu Server

### Requisitos

| Componente | Mínimo recomendado |
|---|---|
| OS | Ubuntu Server 20.04 LTS o 22.04 LTS |
| CPU | 4 núcleos (el modelo corre en CPU) |
| RAM | 4 GB |
| Disco | 10 GB libres |
| Red | IP fija en la VLAN de las tabletas |

---

### Paso 1 — Preparar el servidor

Conéctate al servidor por SSH y asegúrate de tener acceso root o sudo:

```bash
ssh usuario@<IP-del-servidor>
sudo apt-get update && sudo apt-get upgrade -y
```

---

### Paso 2 — Copiar los archivos

Desde tu máquina de desarrollo transfiere el proyecto al servidor:

```bash
scp -r AsistIA/ usuario@<IP-del-servidor>:/tmp/asistia-install
```

O clona el repositorio directamente en el servidor si está en Git:

```bash
git clone <url-del-repo> /tmp/asistia-install
```

---

### Paso 3 — Ejecutar el instalador

```bash
cd /tmp/asistia-install
chmod +x install.sh
sudo ./install.sh
```

El script realiza automáticamente los pasos 4 al 10 descritos a continuación. Si prefieres instalación manual, sigue desde el paso 4.

Al terminar mostrará la IP del servidor y la URL de acceso:

```
╔══════════════════════════════════════════════╗
║         Instalación completada ✓             ║
╚══════════════════════════════════════════════╝

  IP del servidor:   http://192.168.1.10
  Hostname local:    http://asistia.local
```

---

### Pasos detallados (instalación manual)

#### Paso 4 — Dependencias del sistema

```bash
sudo apt-get install -y \
    python3 python3-pip python3-venv \
    libgl1-mesa-glx libglib2.0-0 \
    libsm6 libxext6 libxrender-dev \
    nginx git curl ufw
```

#### Paso 5 — Usuario y directorios

```bash
sudo useradd -r -s /bin/bash -d /opt/asistia -m asistia
sudo mkdir -p /opt/asistia/{static,models,logs}
sudo cp server.py  /opt/asistia/
sudo cp kiosk.html /opt/asistia/static/
sudo chown -R asistia:asistia /opt/asistia
```

#### Paso 6 — Entorno virtual Python

```bash
sudo -u asistia python3 -m venv /opt/asistia/venv
sudo -u asistia /opt/asistia/venv/bin/pip install \
    fastapi==0.111.0 \
    uvicorn[standard]==0.29.0 \
    insightface==0.7.3 \
    opencv-python-headless==4.9.0.80 \
    numpy==1.26.4 \
    pillow==10.3.0 \
    onnxruntime==1.18.0 \
    websockets==12.0 \
    python-multipart==0.0.9
```

#### Paso 7 — Descargar el modelo InsightFace

La primera ejecución descarga ~30 MB del modelo `buffalo_sc`. Requiere internet solo esta vez:

```bash
sudo -u asistia /opt/asistia/venv/bin/python3 -c "
import insightface
app = insightface.app.FaceAnalysis(
    name='buffalo_sc',
    root='/opt/asistia/models',
    providers=['CPUExecutionProvider']
)
app.prepare(ctx_id=0, det_size=(320, 320))
print('Modelo listo')
"
```

Los archivos quedan en `/opt/asistia/models/buffalo_sc/`:
- `det_500m.onnx` — detección facial
- `w600k_mbf.onnx` — reconocimiento (embeddings)

#### Paso 8 — Servicio systemd

Crear `/etc/systemd/system/asistia.service`:

```ini
[Unit]
Description=AsistIA - Control de Asistencia Facial
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=asistia
WorkingDirectory=/opt/asistia
ExecStart=/opt/asistia/venv/bin/uvicorn server:app \
    --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5
StandardOutput=append:/opt/asistia/logs/asistia.log
StandardError=append:/opt/asistia/logs/asistia.log

[Install]
WantedBy=multi-user.target
```

> **Nota:** usar `--workers 1` — SQLite no soporta escrituras concurrentes.

Activar e iniciar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable asistia
sudo systemctl start asistia
sudo systemctl status asistia   # debe mostrar "active (running)"
```

#### Paso 9 — Nginx como reverse proxy

Crear `/etc/nginx/sites-available/asistia`:

```nginx
server {
    listen 80;
    server_name _;   # acepta cualquier hostname / IP

    access_log /opt/asistia/logs/nginx_access.log;
    error_log  /opt/asistia/logs/nginx_error.log;

    location / {
        root /opt/asistia/static;
        index kiosk.html;
        try_files $uri $uri/ /kiosk.html;
    }

    location /api/ {
        proxy_pass       http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /ws/ {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
    }
}
```

Activar y recargar:

```bash
sudo ln -sf /etc/nginx/sites-available/asistia /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

#### Paso 10 — Firewall

```bash
sudo ufw --force enable
sudo ufw allow ssh
sudo ufw allow 80/tcp
sudo ufw status
```

---

### Verificación

```bash
# El servicio está corriendo
systemctl is-active asistia

# Prueba HTTP desde el propio servidor
curl -s http://localhost/api/health | python3 -m json.tool

# Ver logs en tiempo real
journalctl -u asistia -f
```

Desde una tableta en la misma red abrir: `http://<IP-del-servidor>/kiosk.html`

---

### Actualizar la aplicación

Cuando hay cambios en `server.py` o `kiosk.html`:

```bash
sudo cp /ruta/nuevo/server.py  /opt/asistia/server.py
sudo cp /ruta/nuevo/kiosk.html /opt/asistia/static/kiosk.html
sudo systemctl restart asistia
```

---

### Instalación manual (desarrollo local)

```bash
pip install fastapi uvicorn[standard] insightface opencv-python-headless numpy pillow onnxruntime websockets python-multipart
cd AsistIA
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Abrir en el navegador: `http://localhost:8000/static/kiosk.html`

---

## Despliegue con Coolify

Coolify es una plataforma self-hosted que permite desplegar la aplicación desde el repositorio de GitHub con un par de clics, sin tocar la terminal del servidor. Incluye proxy automático (HTTPS), variables de entorno y volúmenes persistentes.

### Requisitos previos

- Coolify instalado en un VPS o servidor propio ([docs.coolify.io](https://docs.coolify.io))
- Repositorio de GitHub conectado a Coolify
- Al menos **2 GB de RAM** en el servidor Coolify (el modelo InsightFace ocupa ~500 MB en memoria)

---

### Paso 1 — Conectar el repositorio

1. En Coolify abre **Projects → + New Project**
2. Selecciona **Add Resource → Public Repository** (o Private si el repo es privado)
3. Pega la URL del repo: `https://github.com/jhon507-c/AsistIA`
4. Branch: `master`

---

### Paso 2 — Configurar el servicio

En la pantalla de configuración del recurso:

| Campo | Valor |
|---|---|
| **Build Pack** | `Dockerfile` |
| **Dockerfile path** | `Dockerfile` |
| **Port** | `8000` |
| **Base directory** | `/` |

Coolify detecta el `Dockerfile` automáticamente si está en la raíz.

---

### Paso 3 — Variables de entorno

En la pestaña **Environment Variables** agrega:

| Variable | Valor | Descripción |
|---|---|---|
| `DB_PATH` | `/data/asistia.db` | Ruta de la base de datos dentro del contenedor |
| `MODELS_DIR` | `/app/models` | Directorio del modelo InsightFace |
| `RECOGNITION_THRESHOLD` | `0.45` | Sensibilidad del reconocimiento (opcional) |
| `LATE_HOUR` | `7` | Hora límite de asistencia (opcional) |
| `LATE_MINUTE` | `15` | Minuto límite (opcional) |

---

### Paso 4 — Volumen persistente

La base de datos y los modelos deben sobrevivir reinicios del contenedor. En la pestaña **Storages** agrega dos volúmenes:

| Nombre | Ruta en contenedor | Descripción |
|---|---|---|
| `asistia-data` | `/data` | Base de datos SQLite |
| `asistia-models` | `/app/models` | Modelo InsightFace (~30 MB) |

> **Importante:** Sin el volumen `/data` la base de datos se borra cada vez que el contenedor se reinicia.

---

### Paso 5 — Configurar el proxy (WebSocket)

AsistIA usa WebSockets para el reconocimiento en tiempo real. En la pestaña **Network** o **Proxy** de Coolify:

1. Asegúrate de que el dominio esté apuntando al servidor
2. Activa **WebSocket support** si aparece como opción
3. En **Advanced → Custom Nginx / Traefik config** agrega si es necesario:

```
# Para Traefik (Coolify v4)
traefik.http.middlewares.asistia-ws.headers.customrequestheaders.Upgrade=websocket
traefik.http.middlewares.asistia-ws.headers.customrequestheaders.Connection=Upgrade
```

En la mayoría de instalaciones de Coolify v4 el WebSocket funciona sin configuración extra.

---

### Paso 6 — Primer despliegue

1. Haz clic en **Deploy**
2. Coolify construye la imagen Docker (la primera vez tarda ~5 min por la descarga del modelo InsightFace)
3. Cuando el estado cambia a **Running** abre la URL asignada

```
https://asistia.tudominio.com/kiosk.html
```

---

### Actualizar la aplicación

Cada vez que hagas `git push` al repo:

- **Opción A (automática):** activa el webhook en Coolify → **Settings → Webhooks → Enable automatic deployment**. Coolify redespliega en cada push al branch `master`.

- **Opción B (manual):** en el dashboard de Coolify haz clic en **Redeploy**.

---

### Notas para Coolify

- **Cámara:** el navegador de la tableta debe acceder a la app por **HTTPS** para que `getUserMedia` (acceso a cámara) funcione. Coolify genera certificados SSL automáticamente con Let's Encrypt si el dominio está configurado.
- **SQLite en contenedor:** el volumen `/data` debe estar montado antes del primer arranque, de lo contrario la DB se crea en la capa temporal del contenedor y se pierde al reiniciar.
- **Primera build:** el `Dockerfile` descarga el modelo InsightFace durante la construcción de la imagen (`RUN python3 -c "..."`) y lo deja en la imagen. El volumen de modelos sirve como caché extra para builds futuras.
- **Logs:** en Coolify ve a **Logs → Container Logs** para ver los logs de uvicorn en tiempo real.

---

## Estructura de archivos

```
AsistIA/
├── server.py          # Backend completo (FastAPI)
├── kiosk.html         # Frontend completo (single-file)
├── install.sh         # Instalación automática Ubuntu Server
├── asistia.db         # Base de datos SQLite (auto-creada)
├── models/            # Modelos InsightFace (auto-descargados)
│   └── buffalo_sc/
│       ├── det_500m.onnx      # Detección facial
│       └── w600k_mbf.onnx     # Reconocimiento (embeddings)
└── static/
    └── kiosk.html     # Copia servida por FastAPI
```

---

## Base de datos

```sql
students (
  id            TEXT PRIMARY KEY,     -- "s_{timestamp_ms}"
  name          TEXT NOT NULL,
  cedula        TEXT DEFAULT '',
  nivel         TEXT DEFAULT '',
  grade         TEXT NOT NULL,
  has_biometric INTEGER DEFAULT 0,    -- 1 si tiene embeddings registrados
  created_at    TEXT NOT NULL
)

face_embeddings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id  TEXT REFERENCES students(id) ON DELETE CASCADE,
  embedding   BLOB NOT NULL,          -- np.float32 bytes (512 dimensiones)
  created_at  TEXT NOT NULL
)

attendance (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id   TEXT REFERENCES students(id),
  student_name TEXT NOT NULL,
  grade        TEXT NOT NULL,
  date         TEXT NOT NULL,         -- "YYYY-MM-DD"
  time         TEXT NOT NULL,         -- "HH:MM"
  status       TEXT NOT NULL,         -- 'present' | 'late' | 'already_today'
  confidence   REAL,
  UNIQUE(student_id, date)
)
```

---

## API REST

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET`    | `/api/health` | Estado del servidor y modelo |
| `GET`    | `/api/students?q=` | Lista o busca alumnos |
| `POST`   | `/api/students` | Crea alumno con embeddings |
| `PATCH`  | `/api/students/{id}/biometric` | Actualiza biometría de alumno existente |
| `DELETE` | `/api/students/{id}` | Elimina alumno |
| `POST`   | `/api/students/import` | Importación masiva desde CSV |
| `GET`    | `/api/attendance?fecha=` | Asistencia por fecha |
| `GET`    | `/api/attendance/stats?fecha=` | Totales del día |
| `GET`    | `/api/attendance/export?fecha=` | Descarga CSV de asistencia |
| `GET`    | `/api/stats/dashboard?days=7` | Datos para gráficas del dashboard |

---

## WebSocket Protocols

### `/ws/kiosk` — Reconocimiento

```
Cliente → { "frame": "<base64 JPEG 320x240>" }

Servidor → {
  "type": "match" | "no_face" | "unknown" | "already_marked" | "error",
  "student": { "id", "name", "grade" },   // solo en "match"
  "confidence": float,                     // 0–100
  "status": "present" | "late" | "already_today",
  "time": "HH:MM"
}
```

El cliente usa patrón **request-response** (no setInterval): envía el siguiente frame solo al recibir respuesta, evitando queue buildup en CPU lento.

### `/ws/register` — Registro biométrico

```
Cliente → { "frame": "<base64 JPEG>", "phase": 0|1|2 }

Servidor → {
  "type": "detected" | "no_face" | "poor_quality" | "wrong_pose",
  "quality": float,    // 0–100
  "embedding": [...],  // 512 floats, solo en "detected"
  "bbox": [x1,y1,x2,y2]
}
```

---

## Flujos principales

### Reconocimiento (kiosco)
1. Tableta abre `kiosk.html` → pantalla de carga conecta al servidor
2. Loop WS envía frame → servidor detecta rostro → calcula distancia coseno contra cache
3. Si distancia ≤ 0.45 → match, registra asistencia en SQLite
4. Resultado aparece en overlay 4 segundos, luego siguiente frame

### Registro de alumno (manual)
1. Admin → "Registrar Alumno" → llena nombre y grado
2. "Iniciar cámara" → instrucciones por fase (frente / izquierda / derecha)
3. Presiona "📸 Capturar foto" en cada fase → 3 embeddings guardados
4. "Registrar Alumno" → POST `/api/students`

### Importación masiva (CSV)
1. Admin → arrastra CSV con columnas: `nombre, cedula, nivel`
2. Preview de las primeras 5 filas
3. "Confirmar importación" → POST `/api/students/import`
4. Alumnos quedan con `has_biometric=0`
5. Usar "Completar biometría" para buscar y agregar fotos posteriormente

---

## Configuración

| Variable | Default | Descripción |
|----------|---------|-------------|
| `RECOGNITION_THRESHOLD` | `0.45` | Distancia coseno máxima para match (bajar = más estricto) |
| `LATE_HOUR` | `7` | Hora límite de llegada a tiempo |
| `LATE_MINUTE` | `15` | Minuto límite (7:15 AM) |
| `COOLDOWN_SECONDS` | `8` | Segundos antes de re-marcar el mismo rostro |

---

## Comandos útiles (producción)

```bash
# Ver logs en vivo
journalctl -u asistia -f

# Reiniciar servicio
sudo systemctl restart asistia

# Estado
sudo systemctl status asistia

# Backup de la base de datos
cp /opt/asistia/asistia.db /backup/asistia_$(date +%Y%m%d).db

# Actualizar kiosk.html
sudo cp /ruta/nuevo/kiosk.html /opt/asistia/static/
sudo systemctl restart asistia
```

---

## Paleta de diseño

| Token | Valor | Uso |
|-------|-------|-----|
| `--bg` | `#07110d` | Fondo de página |
| `--surface` | `#0d1f17` | Cards, panels |
| `--accent` | `#22c55e` | Verde principal — botones, anillo, highlights |
| `--accent2` | `#4ade80` | Verde claro — gradientes, hover |
| `--warn` | `#fbbf24` | Estado tardío, advertencias |
| `--danger` | `#f43f5e` | Errores, estado desconocido |
| `--text` | `#ecfdf5` | Texto principal |
| `--muted` | `#4b7a5e` | Texto secundario |

Fuentes: **Syne** (headings/números) + **DM Sans** (body) vía Google Fonts.

---

## Notas técnicas

- **buffalo_sc** solo incluye detección (`det_500m`) y reconocimiento (`w600k_mbf`). No tiene estimación de pose — el registro es manual (el usuario gira la cabeza y presiona el botón).
- **Modo demo:** si InsightFace no carga, el servidor sigue funcionando. El WS del kiosco responde `no_face` a cada frame.
- **SQLite en producción:** usar `--workers 1` en uvicorn (SQLite no soporta escrituras concurrentes). Para mayor carga migrar a PostgreSQL.
- **Nginx:** configurar `proxy_read_timeout 86400` en el bloque `/ws/` para mantener conexiones WebSocket activas 24h.
