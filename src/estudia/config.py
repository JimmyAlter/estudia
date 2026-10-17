"""Rutas y constantes de la aplicación."""
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

# Tope de espera por pedido a Gemini (el video tarda).
GEMINI_TIMEOUT_S = 300

# Sincronización automática como máximo cada 6 horas.
SYNC_EVERY_S = 21600

SECRETS_SERVICE = "estudia"


def data_path() -> Path:
    """Dónde van los datos del usuario, sin crear nada."""
    return Path(platformdirs.user_data_dir("estudia"))


def data_dir() -> Path:
    """Carpeta de datos del usuario; se crea si no existe."""
    path = data_path()
    path.mkdir(parents=True, exist_ok=True)
    return path
