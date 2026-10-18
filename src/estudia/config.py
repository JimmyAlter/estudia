"""Rutas y constantes de la aplicación."""
import os
import sys
from pathlib import Path

import platformdirs

# Cadenas de modelos, en orden de preferencia. El plan gratuito da 20 pedidos por día
# a cada modelo: cuando uno se agota se pasa al siguiente. Al usarlas se cruzan con
# client.models.list() (lo que esa clave puede usar de verdad).
# El análisis manda video: solo modelos que lo aceptan.
ANALYZE_MODELS = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite",
                  "gemini-2.5-flash-lite"]
# Tope de una marca de "modelo agotado por hoy": la cuota diaria vuelve como mucho
# en un día (más una hora de margen). Una marca más lejana es un error y no se usa.
MAX_EXHAUSTED_S = 86400 + 3600

# Un análisis guardado sin modelo (de antes de registrarlo) lo hizo este, el único
# que se usaba entonces.
LEGACY_ANALYSIS_MODEL = "gemini-2.5-flash"
# La pasada a ciegas usa otra familia o gama que el análisis (y nunca el mismo modelo
# que analizó ese material): el control es independiente y reparte la cuota.
BLIND_MODELS = ["gemini-3.5-flash-lite", "gemma-4-31b-it", "gemini-2.5-flash-lite",
                "gemini-3.1-flash-lite"]

# Si Gemini no acepta enlaces de YouTube con una clave (403 en toda la cadena), no se
# vuelve a probar el enlace durante 7 días; después se prueba de nuevo.
YOUTUBE_URL_DENIED_S = 7 * 86400
# Tope de video de YouTube que se manda a Gemini por día local (el plan gratuito
# admite 8 h de YouTube por día). El día es el de la cuota de Gemini (Pacífico).
DAILY_VIDEO_CAP_S = 8 * 3600
# Duración que se supone para un video antes de mandarlo: el campus da solo el id de
# YouTube y averiguar la duración real pediría a YouTube (o a yt-dlp). Después del
# análisis se cuenta la duración que informa Gemini.
VIDEO_ESTIMATE_S = 3600

# Tope de espera por pedido a Gemini (el video tarda).
GEMINI_TIMEOUT_S = 300

# Sincronización automática como máximo cada 6 horas.
SYNC_EVERY_S = 21600

SECRETS_SERVICE = "estudia"


# Variable de entorno que fija la carpeta de datos (los tests la usan para no tocar
# nunca los datos reales; en Windows platformdirs no lee APPDATA sino la API del sistema).
DATA_DIR_ENV = "ESTUDIA_DATA_DIR"


def data_path() -> Path:
    """Dónde van los datos del usuario, sin crear nada."""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir("estudia"))


def data_dir() -> Path:
    """Carpeta de datos del usuario; se crea si no existe."""
    path = data_path()
    path.mkdir(parents=True, exist_ok=True)
    return path


# Runtime de JavaScript que el instalador de Windows trae para yt-dlp (ver
# packaging/windows/estudia.spec). Va en `runtime/` dentro de la carpeta congelada.
BUNDLED_JS_RUNTIME = "node"


def bundled_js_runtime() -> Path | None:
    """El binario del runtime de JS incluido en el ejecutable congelado, si está."""
    if not getattr(sys, "frozen", False):
        return None
    exe = BUNDLED_JS_RUNTIME + (".exe" if sys.platform == "win32" else "")
    path = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "runtime" / exe
    return path if path.is_file() else None
