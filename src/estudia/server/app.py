"""Servidor local: API JSON sobre el núcleo de Estudia.

Escucha solo en 127.0.0.1. Los secretos (token de Moodle, clave de IA) nunca
aparecen en respuestas, mensajes de error ni logs.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from secrets import token_hex
from typing import Any

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from estudia.ai.base import (
    AIAuthError,
    AIFormatError,
    AIProvider,
    AIQuotaError,
    AIUnavailableError,
)
from estudia.catalog import get_site, load_catalog
from estudia.config import SYNC_EVERY_S, data_dir
from estudia.db import connect
from estudia.generation.pipeline import (
    QUOTA_RESET_KEY,
    RETRY_ON_NEW_KEY,
    STOP_KEY,
    URL_INPUT_KEY,
    forget_exhausted_models,
    process_pending,
    retry_failed,
)
from estudia.moodle.auth import AuthError, launch_url, parse_callback
from estudia.moodle.client import MoodleClient, MoodleError, TokenInvalid
from estudia.moodle.sync import sync_all
from estudia.secrets import delete_secret, get_secret, set_secret
from estudia.study.home import home_view, next_exam
from estudia.study.practice import (
    build_quiz,
    record_answer,
    retire_question,
    unretire_question,
)

HOST = "127.0.0.1"
GEMINI_KEY = "gemini_key"
SITE_KEY = "site_id"
SYNC_ERROR_KEY = "last_sync_error"
# Las escrituras esperan a que termine la sincronización en curso en vez de fallar.
BUSY_TIMEOUT_MS = 30000

log = logging.getLogger(__name__)

MoodleFactory = Callable[[str, str], Any]
ProviderFactory = Callable[[str], AIProvider]


def moodle_token_name(site_id: str) -> str:
    return f"moodle_token:{site_id}"


def moodle_private_name(site_id: str) -> str:
    return f"moodle_private:{site_id}"


def web_dist_dir() -> Path:
    """Carpeta del frontend compilado (`web/dist`), en desarrollo o empaquetado."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "web" / "dist"
    # src/estudia/server/app.py -> raíz del proyecto
    return Path(__file__).resolve().parents[3] / "web" / "dist"


def default_moodle_factory() -> MoodleFactory:
    http: httpx.Client | None = None

    def factory(site_url: str, token: str) -> MoodleClient:
        nonlocal http
        if http is None:
            http = httpx.Client(timeout=60)
        return MoodleClient(site_url, token, http)

    return factory


def default_provider_factory(key: str) -> AIProvider:
    from estudia.ai.gemini import GeminiProvider

    return GeminiProvider(key)


def _kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def _kv_delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM kv WHERE key=?", (key,))
    conn.commit()


class Jobs:
    """Sincronización + procesamiento en segundo plano; nunca dos a la vez."""

    def __init__(self, db_path: Path, moodle_client_factory: MoodleFactory,
                 ai_provider_factory: ProviderFactory):
        self.db_path = db_path
        self.moodle_client_factory = moodle_client_factory
        self.ai_provider_factory = ai_provider_factory
        self._guard = threading.Lock()
        self._running = False
        self._again = False

    def connect(self) -> sqlite3.Connection:
        conn = connect(self.db_path, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    @property
    def running(self) -> bool:
        return self._running

    def try_start(self, *, queue: bool = False) -> bool:
        """Reserva la corrida. Si ya hay una y `queue`, la actual se repite al terminar."""
        with self._guard:
            if self._running:
                if queue:
                    self._again = True
                return False
            self._running = True
            return True

    def finish(self) -> bool:
        """Libera la corrida; devuelve True si quedó una repetición pedida (y la mantiene)."""
        with self._guard:
            if self._again:
                self._again = False
                return True
            self._running = False
            return False

    def run_reserved(self) -> None:
        """Corre lo reservado con `try_start` (y las repeticiones pedidas)."""
        try:
            while True:
                self._run_once()
                if not self.finish():
                    return
        except BaseException:
            self.finish_all()
            raise

    def finish_all(self) -> None:
        with self._guard:
            self._again = False
            self._running = False

    def run_now(self) -> bool:
        """Corre en este hilo si no hay otra corrida; False si estaba ocupado."""
        if not self.try_start():
            return False
        self.run_reserved()
        return True

    def seconds_until_due(self) -> float:
        """Segundos hasta la próxima sincronización automática (0 si ya toca)."""
        conn = self.connect()
        try:
            last = _kv_get(conn, "last_sync_at")
        finally:
            conn.close()
        if last is None:
            return 0
        return max(0.0, int(last) + SYNC_EVERY_S - time.time())

    def _run_once(self) -> None:
        conn = self.connect()
        try:
            self._sync(conn)
            self._process(conn)
        finally:
            conn.close()

    def _sync(self, conn: sqlite3.Connection) -> None:
        site_id = _kv_get(conn, SITE_KEY)
        token = get_secret(moodle_token_name(site_id)) if site_id else None
        if not site_id or not token:
            return
        error = None
        try:
            client = self.moodle_client_factory(get_site(site_id).url, token)
            sync_all(conn, client, site_id)
        except TokenInvalid:
            log.warning("Moodle rechazó el token: hay que volver a iniciar sesión")
            forget_moodle_session(site_id)
            error = "token"
        except MoodleError as exc:
            # Los mensajes de MoodleError ya vienen sin token.
            log.warning("No se pudo sincronizar con Moodle: %s", exc)
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - un hilo de fondo no debe morir
            log.warning("Falló la sincronización (%s)", type(exc).__name__)
            error = type(exc).__name__
        if error is None:
            _kv_delete(conn, SYNC_ERROR_KEY)
        else:
            _kv_set(conn, SYNC_ERROR_KEY, error)

    def _process(self, conn: sqlite3.Connection) -> None:
        key = get_secret(GEMINI_KEY)
        # Con la clave rechazada se espera a que el usuario cargue otra.
        if not key or _kv_get(conn, STOP_KEY) == "auth":
            return
        try:
            process_pending(conn, self.ai_provider_factory(key))
        except Exception as exc:  # noqa: BLE001
            log.warning("Falló el procesamiento (%s)", type(exc).__name__)


def forget_moodle_session(site_id: str) -> None:
    delete_secret(moodle_token_name(site_id))
    delete_secret(moodle_private_name(site_id))


def periodic_sync(jobs: Jobs, stop: threading.Event) -> None:
    """Sincroniza cada SYNC_EVERY_S mientras la app está abierta (la primera, cuando toca)."""
    delay: float | None = None
    while True:
        try:
            if delay is None:
                delay = jobs.seconds_until_due()
            if stop.wait(delay):
                return
            delay = SYNC_EVERY_S
            jobs.run_now()
        except Exception as exc:  # noqa: BLE001 - el hilo no debe morir
            log.warning("Falló la sincronización periódica (%s)", type(exc).__name__)
            delay = SYNC_EVERY_S


LOCAL_HOSTS = ["127.0.0.1", "localhost"]


class OriginGuard:
    """Rechaza pedidos que cambian algo si vienen de otra página (Origin ajeno).

    Sin Origin se aceptan: así llega el handler de `estudia://`, que no es un navegador.
    """

    def __init__(self, app, port: int | None = None):
        self.app = app
        self.port = port

    def _allowed(self, host: str | None) -> set[str]:
        if self.port is not None:
            return {f"http://{h}:{self.port}" for h in LOCAL_HOSTS}
        # Sin puerto configurado: el mismo origen que el Host (ya validado como local).
        return {f"http://{host}"} if host else set()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD"):
            headers = {k.lower(): v.decode("latin-1") for k, v in scope["headers"]}
            origin = headers.get(b"origin")
            if origin is not None and origin not in self._allowed(headers.get(b"host")):
                response = JSONResponse({"detail": "origen no permitido"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class SiteIn(BaseModel):
    site_id: str


class CallbackIn(BaseModel):
    url: str


class AIKeyIn(BaseModel):
    provider: str
    key: str


class QuizIn(BaseModel):
    topic_id: int | None = None
    weakest: bool = False


class AnswerIn(BaseModel):
    question_id: int
    chosen_index: int


def _quiz_course(conn: sqlite3.Connection, topic_id: int | None, now: int) -> int | None:
    """Curso del cuestionario: el del tema pedido, el del próximo examen o el primero con preguntas."""
    if topic_id is not None:
        row = conn.execute("SELECT course_id FROM topic WHERE id=?", (topic_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "tema inexistente")
        return row[0]
    exam = next_exam(conn, now)
    if exam is not None:
        return exam["course_id"]
    row = conn.execute(
        "SELECT MIN(t.course_id) FROM topic t JOIN question q ON q.topic_id = t.id"
        " WHERE q.verified = 1 AND q.retired = 0"
    ).fetchone()
    return row[0]


def _campus_url(conn: sqlite3.Connection) -> str | None:
    """URL base del campus elegido, solo si es http(s)."""
    site_id = _kv_get(conn, SITE_KEY)
    if not site_id:
        return None
    try:
        url = get_site(site_id).url.rstrip("/")
    except KeyError:
        return None
    return url if url.startswith(("https://", "http://")) else None


def create_app(
    *,
    db_path: Path | None = None,
    moodle_client_factory: MoodleFactory | None = None,
    ai_provider_factory: ProviderFactory | None = None,
    open_browser: Callable[[str], Any] | None = None,
    web_dist: Path | None = None,
    port: int | None = None,
) -> FastAPI:
    if db_path is None:
        db_path = data_dir() / "estudia.db"
    if open_browser is None:
        import webbrowser

        open_browser = webbrowser.open
    jobs = Jobs(
        db_path,
        moodle_client_factory or default_moodle_factory(),
        ai_provider_factory or default_provider_factory,
    )
    # Passport del login en curso: solo en memoria.
    login = {"passport": None}

    app = FastAPI(title="Estudia", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobs = jobs
    # Contra DNS rebinding y pedidos de otros sitios (el último agregado va por fuera).
    app.add_middleware(OriginGuard, port=port)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # La respuesta por defecto repite la entrada (podría ser una clave o el callback).
        return JSONResponse({"detail": "datos inválidos"}, status_code=422)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = jobs.connect()
        try:
            yield conn
        finally:
            conn.close()

    def require_session(conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Connection:
        site_id = _kv_get(conn, SITE_KEY)
        if not site_id or not get_secret(moodle_token_name(site_id)):
            raise HTTPException(401, "hay que iniciar sesión en Moodle")
        return conn

    def start_jobs(background: BackgroundTasks) -> None:
        if jobs.try_start(queue=True):
            background.add_task(jobs.run_reserved)

    @app.get("/api/state")
    def state(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        site_id = _kv_get(conn, SITE_KEY)
        stop_reason = _kv_get(conn, STOP_KEY)
        if not site_id:
            step = "facultad"
        elif not get_secret(moodle_token_name(site_id)):
            step = "login"
        elif not get_secret(GEMINI_KEY) or stop_reason == "auth":
            step = "ia"
        else:
            step = "listo"
        last = _kv_get(conn, "last_sync_at")
        resets_at = _kv_get(conn, QUOTA_RESET_KEY)
        return {
            "step": step,
            "site_id": site_id,
            "ai_stop_reason": stop_reason,
            # Con cuota agotada: cuándo vuelve el primer modelo (epoch), si se sabe.
            "ai_quota_resets_at": int(resets_at) if resets_at and stop_reason == "quota" else None,
            "syncing": jobs.running,
            "last_sync_at": int(last) if last else None,
            "last_sync_error": _kv_get(conn, SYNC_ERROR_KEY),
        }

    @app.get("/api/catalog")
    def catalog() -> list[dict]:
        return [{"id": s.id, "name": s.name, "university": s.university} for s in load_catalog()]

    @app.post("/api/site")
    def choose_site(body: SiteIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            get_site(body.site_id)
        except KeyError:
            raise HTTPException(404, "facultad desconocida") from None
        _kv_set(conn, SITE_KEY, body.site_id)
        login["passport"] = None
        return {"ok": True}

    @app.post("/api/login/start")
    def login_start(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        site_id = _kv_get(conn, SITE_KEY)
        if not site_id:
            raise HTTPException(409, "primero hay que elegir la facultad")
        passport = token_hex(16)
        login["passport"] = passport
        url = launch_url(get_site(site_id), passport)
        open_browser(url)
        return {"url": url}

    @app.post("/api/login/callback")
    def login_callback(body: CallbackIn, background: BackgroundTasks,
                       conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        site_id = _kv_get(conn, SITE_KEY)
        passport = login["passport"]
        if not site_id or not passport:
            raise HTTPException(400, "no hay un inicio de sesión en curso")
        try:
            pair = parse_callback(body.url)
        except AuthError:
            raise HTTPException(400, "respuesta de inicio de sesión inválida") from None
        expected = hashlib.md5((get_site(site_id).url + passport).encode()).hexdigest()
        if not pair.signature or not hmac.compare_digest(pair.signature.lower(), expected):
            raise HTTPException(400, "la respuesta no corresponde a este inicio de sesión")
        login["passport"] = None
        set_secret(moodle_token_name(site_id), pair.token)
        if pair.private_token:
            set_secret(moodle_private_name(site_id), pair.private_token)
        else:
            delete_secret(moodle_private_name(site_id))
        start_jobs(background)
        return {"ok": True}

    @app.post("/api/ai")
    def set_ai_key(body: AIKeyIn, background: BackgroundTasks,
                   conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if body.provider != "gemini":
            raise HTTPException(400, "proveedor no soportado")
        key = body.key.strip()
        if not key:
            raise HTTPException(400, "falta la clave")
        try:
            jobs.ai_provider_factory(key).check_key()
        except (AIAuthError, AIFormatError):
            # Un pedido inválido tampoco prueba que la clave sirva.
            raise HTTPException(400, "la clave fue rechazada") from None
        except AIUnavailableError:
            raise HTTPException(503, "no se pudo verificar la clave; probá más tarde") from None
        except AIQuotaError:
            pass  # la clave autenticó: solo se agotó la cuota
        set_secret(GEMINI_KEY, key)
        _kv_delete(conn, STOP_KEY)
        _kv_delete(conn, QUOTA_RESET_KEY)
        # La cuota es por proyecto: otra clave puede tener los modelos disponibles.
        forget_exhausted_models(conn)
        # Con otra clave puede que Gemini sí acepte enlaces de YouTube: se vuelve a probar.
        _kv_delete(conn, URL_INPUT_KEY)
        # Lo que falló con la clave anterior se vuelve a intentar con la nueva
        # (no los videos que Gemini no pudo abrir: eso no depende de la clave).
        retry_failed(conn, RETRY_ON_NEW_KEY)
        start_jobs(background)
        return {"ok": True}

    @app.post("/api/sync", status_code=202)
    def sync(background: BackgroundTasks,
             conn: sqlite3.Connection = Depends(require_session)) -> dict:
        if not jobs.try_start():
            raise HTTPException(409, "ya hay una sincronización en curso")
        background.add_task(jobs.run_reserved)
        return {"started": True}

    @app.get("/api/home")
    def home(conn: sqlite3.Connection = Depends(get_conn)):
        # Datos locales: con la sesión del campus vencida el inicio sigue andando.
        return home_view(conn, int(time.time()))

    @app.get("/api/materials")
    def materials(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        site_url = _campus_url(conn)
        sections: dict[int, dict] = {}
        rows = conn.execute(
            "SELECT s.id AS section_id, s.course_id, s.num, s.name AS section_name,"
            " c.fullname AS course, m.id, m.cmid, m.title, m.kind, m.status, m.summary_md, m.error, m.ref"
            " FROM section s"
            " LEFT JOIN course c ON c.id = s.course_id"
            " JOIN material m ON m.section_id = s.id"
            " WHERE s.visible = 1 ORDER BY s.course_id, s.num, m.id"
        )
        for r in rows:
            section = sections.setdefault(r["section_id"], {
                "id": r["section_id"], "course_id": r["course_id"], "course": r["course"],
                "num": r["num"], "name": r["section_name"], "materials": [],
            })
            section["materials"].append({
                "id": r["id"], "title": r["title"], "kind": r["kind"], "status": r["status"],
                "summary_md": r["summary_md"], "error": r["error"],
                "youtube_id": r["ref"] if r["kind"] == "youtube" else None,
                "campus_url": (f"{site_url}/mod/page/view.php?id={r['cmid']}"
                               if site_url and r["cmid"] else None),
            })
        return list(sections.values())

    @app.post("/api/materials/retry-failed")
    def retry_failed_materials(background: BackgroundTasks,
                               conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        reset = retry_failed(conn)
        if reset:
            start_jobs(background)
        return {"reset": reset}

    @app.post("/api/quiz")
    def quiz(body: QuizIn, conn: sqlite3.Connection = Depends(get_conn)):
        now = int(time.time())
        course_id = _quiz_course(conn, body.topic_id, now)
        if course_id is None:
            return []
        return build_quiz(conn, course_id, topic_id=body.topic_id, weakest=body.weakest, now=now)

    @app.post("/api/answer")
    def answer(body: AnswerIn, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            return record_answer(conn, body.question_id, body.chosen_index, int(time.time()))
        except LookupError:
            raise HTTPException(404, "pregunta no disponible") from None
        except ValueError:
            raise HTTPException(400, "opción fuera de rango") from None

    @app.post("/api/question/{question_id}/retire")
    def retire(question_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if conn.execute("SELECT 1 FROM question WHERE id=?", (question_id,)).fetchone() is None:
            raise HTTPException(404, "pregunta inexistente")
        retire_question(conn, question_id)
        return {"ok": True}

    @app.post("/api/question/{question_id}/unretire")
    def unretire(question_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if conn.execute("SELECT 1 FROM question WHERE id=?", (question_id,)).fetchone() is None:
            raise HTTPException(404, "pregunta inexistente")
        unretire_question(conn, question_id)
        return {"ok": True}

    dist = web_dist if web_dist is not None else web_dist_dir()
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    else:
        @app.get("/", response_class=PlainTextResponse)
        def placeholder() -> str:
            return "Estudia está andando. Falta compilar la interfaz (web/dist)."

    return app
