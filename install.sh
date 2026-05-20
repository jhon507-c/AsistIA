#!/bin/bash
# ============================================================
#  AsistIA — Script de instalación
#  HP ProLiant ML110 Gen10 · Ubuntu Server 20.04/22.04
#  Ejecutar como root o con sudo:
#    chmod +x install.sh && sudo ./install.sh
# ============================================================

set -e  # Detener si hay error

# ── Colores ──────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
info() { echo -e "${YELLOW}→${NC} $1"; }
err()  { echo -e "${RED}✗${NC} $1"; exit 1; }

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║        AsistIA — Instalación         ║"
echo "  ║   Instituto Panamericano de David    ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# ── 1. Sistema base ───────────────────────────────────────────
info "Actualizando sistema..."
apt-get update -qq && apt-get upgrade -y -qq
ok "Sistema actualizado"

# ── 2. Dependencias del sistema ───────────────────────────────
info "Instalando dependencias del sistema..."
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    libgl1-mesa-glx libglib2.0-0 \
    libsm6 libxext6 libxrender-dev \
    nginx certbot python3-certbot-nginx \
    git curl ufw
ok "Dependencias instaladas"

# ── 3. Crear usuario del servicio ─────────────────────────────
info "Creando usuario 'asistia'..."
if ! id "asistia" &>/dev/null; then
    useradd -r -s /bin/bash -d /opt/asistia -m asistia
    ok "Usuario 'asistia' creado"
else
    ok "Usuario 'asistia' ya existe"
fi

# ── 4. Directorio de la aplicación ────────────────────────────
info "Configurando directorio /opt/asistia..."
mkdir -p /opt/asistia/{static,models,logs}
cp server.py  /opt/asistia/
cp kiosk.html /opt/asistia/static/
chown -R asistia:asistia /opt/asistia
ok "Archivos copiados"

# ── 5. Entorno virtual Python ─────────────────────────────────
info "Creando entorno virtual Python..."
sudo -u asistia python3 -m venv /opt/asistia/venv
ok "Entorno virtual creado"

info "Instalando dependencias Python (puede tardar 3-5 min)..."
sudo -u asistia /opt/asistia/venv/bin/pip install --upgrade pip -q
sudo -u asistia /opt/asistia/venv/bin/pip install \
    fastapi==0.111.0 \
    uvicorn[standard]==0.29.0 \
    insightface==0.7.3 \
    opencv-python-headless==4.9.0.80 \
    numpy==1.26.4 \
    pillow==10.3.0 \
    onnxruntime==1.18.0 \
    websockets==12.0 \
    python-multipart==0.0.9 \
    -q
ok "Dependencias Python instaladas"

# ── 6. Descargar modelo InsightFace (primera vez) ─────────────
info "Descargando modelo de reconocimiento facial..."
sudo -u asistia /opt/asistia/venv/bin/python3 - <<'EOF'
import insightface
app = insightface.app.FaceAnalysis(
    name="buffalo_sc",
    root="/opt/asistia/models",
    providers=["CPUExecutionProvider"]
)
app.prepare(ctx_id=0, det_size=(320, 320))
print("Modelo descargado correctamente")
EOF
ok "Modelo InsightFace listo"

# ── 7. Servicio systemd ───────────────────────────────────────
info "Configurando servicio systemd..."
cat > /etc/systemd/system/asistia.service << 'UNIT'
[Unit]
Description=AsistIA - Control de Asistencia Facial
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=asistia
WorkingDirectory=/opt/asistia
ExecStart=/opt/asistia/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5
StandardOutput=append:/opt/asistia/logs/asistia.log
StandardError=append:/opt/asistia/logs/asistia.log
Environment=PYTHONPATH=/opt/asistia

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable asistia
systemctl start asistia
sleep 2

if systemctl is-active --quiet asistia; then
    ok "Servicio AsistIA corriendo"
else
    err "Error iniciando el servicio. Revisa: journalctl -u asistia -n 20"
fi

# ── 8. Nginx como reverse proxy ───────────────────────────────
info "Configurando Nginx..."

# Detectar IP del servidor
SERVER_IP=$(hostname -I | awk '{print $1}')

cat > /etc/nginx/sites-available/asistia << NGINX
server {
    listen 80;
    server_name ${SERVER_IP} asistia.local;

    # Logs
    access_log /opt/asistia/logs/nginx_access.log;
    error_log  /opt/asistia/logs/nginx_error.log;

    # Kiosk frontend
    location / {
        root /opt/asistia/static;
        index kiosk.html;
        try_files \$uri \$uri/ /kiosk.html;
    }

    # API REST
    location /api/ {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
    }

    # WebSocket kiosko
    location /ws/ {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host \$host;
        proxy_read_timeout 86400;    # 24h — conexión persistente
        proxy_send_timeout 86400;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/asistia /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
ok "Nginx configurado"

# ── 9. Firewall ───────────────────────────────────────────────
info "Configurando firewall..."
ufw --force enable
ufw allow ssh
ufw allow 80/tcp     # HTTP — acceso desde tabletas (VLAN)
ufw allow 443/tcp    # HTTPS — opcional futuro
ok "Firewall configurado (SSH + HTTP)"

# ── 10. Resolución DNS local (hostname asistia.local) ─────────
info "Configurando hostname asistia.local..."
echo "127.0.0.1  asistia.local" >> /etc/hosts
# Instalar avahi para resolución mDNS en la red (opcional)
apt-get install -y -qq avahi-daemon
hostnamectl set-hostname asistia
systemctl enable avahi-daemon --now
ok "Hostname asistia.local configurado"

# ── RESUMEN FINAL ─────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         Instalación completada ✓             ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  IP del servidor:   ${YELLOW}http://${SERVER_IP}${NC}"
echo -e "  Hostname local:    ${YELLOW}http://asistia.local${NC}"
echo ""
echo -e "  Desde las tabletas abre:"
echo -e "  ${YELLOW}http://${SERVER_IP}/kiosk.html${NC}"
echo ""
echo -e "  Comandos útiles:"
echo -e "    Ver logs en vivo:  ${YELLOW}journalctl -u asistia -f${NC}"
echo -e "    Reiniciar:         ${YELLOW}systemctl restart asistia${NC}"
echo -e "    Estado:            ${YELLOW}systemctl status asistia${NC}"
echo ""
echo -e "  ⚠  VLAN: Asegúrate que el switch permite tráfico"
echo -e "     del puerto de las tabletas al puerto del servidor"
echo -e "     en el puerto 80 (HTTP)."
echo ""
