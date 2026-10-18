"""Servidor local: API JSON sobre el núcleo de Estudia.

Escucha solo en 127.0.0.1. Los secretos (token de Moodle, clave de IA) nunca
aparecen en respuestas, mensajes de error ni logs.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import hmac
import logging
import sqlite3
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Any

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, StrictInt

from estudia.ai.base import (
    AIAuthError,
    AIFormatError,
    AIProvider,
    AIQuotaError,
    AIUnavailableError,
)
from estudia.ai.gate import AIBlocked, GatedProvider, age_is_adult
from estudia.catalog import get_site, load_catalog
from estudia.config import SYNC_EVERY_S, data_dir
from estudia.db import connect
from estudia.generation.verify import GradingUnavailable
from estudia.generation.pipeline import (
    ALLOW_DOWNLOAD_KEY,
    BUDGET,
    QUOTA_RESET_KEY,
    RETRY_ON_NEW_KEY,
    STOP_KEY,
    URL_INPUT_KEY,
    YOUTUBE_CAP,
    download_allowed,
    forget_exhausted_models,
    process_pending,
    retry_failed,
    retry_needs_link,
)
from estudia.moodle.auth import AuthError, launch_url, parse_callback
from estudia.moodle.calendar_feed import (
    FeedUnreachable,
    campus_calendar_url,
    feed_secret_name,
    feed_status_key,
    fetch_feed_url,
)
from estudia.moodle.client import MoodleClient, MoodleError, TokenInvalid
from estudia.moodle.sync import sync_all
from estudia.updates import default_http, latest_release
from estudia.version import __version__
from estudia.secrets import delete_secret, get_secret, set_secret
from estudia.reminders import KV_ENABLED as REMINDERS_KEY
from estudia.reminders import reminders_enabled
from estudia.study.exams import (
    create_manual_exam,
    delete_manual_exam,
    ENROLLMENT,
    exam_topics,
    list_exams,
    set_exam_result,
    skip_exam_result,
    update_manual_exam,
)
from estudia.study.home import home_view, next_exam
from estudia.study.progress import progress_view
from estudia.study.pretest import build_pretest, close_pretest, record_pretest_answer
from estudia.study.recall import recall_check
from estudia.study.anki import build_apkg, course_slug, weekly_question_ids
from estudia.study.ics import EXAM_MIN, build_ics
from estudia.study.mock_exam import SHORT_NOT_ENOUGH_TEXT, NotEnoughQuestions, finish_mock, mock_result, start_mock
from estudia.study.planner import (
    get_availability,
    mark_done_from_answers,
    plan_health,
    preferred_start_min,
    replan,
    sessions,
    set_availability,
)
from estudia.study.practice import (
    build_quiz,
    build_session_quiz,
    record_answer,
    record_explain_match,
    retire_question,
    unretire_question,
)
from estudia.study.schedule import (
    TZ,
    add_slot,
    delete_slot,
    dismiss_suggestion,
    list_slots,
    occurrences,
    suggestions,
    update_slot,
)
from estudia.study.srs import UNSURE

HOST = "127.0.0.1"
GEMINI_KEY = "gemini_key"
SITE_KEY = "site_id"
SYNC_ERROR_KEY = "last_sync_error"
# Las escrituras esperan a que termine la sincronización en curso en vez de fallar.
BUSY_TIMEOUT_MS = 30000
# Gemini saturado: se reintenta solo el procesamiento a los 5, 10, 20 y después
# cada 30 minutos, hasta que un material sale bien.
RETRY_FIRST_S = 300
RETRY_MAX_S = 1800
# El ciclo periódico se fija cada tanto si una corrida pedida desde la interfaz
# dejó programado un reintento.
POLL_S = 60
# Con respuestas nuevas o material procesado, el plan se rehace como mucho cada 10 minutos.
PLAN_REFRESH_KEY = "plan_refreshed_at"
PLAN_REFRESH_EVERY_S = 600
STUDY_START_KEY = "study_start_min"
# Puerta de edad: "adult" | "minor"; sin valor, todavía no respondió.
AGE_KEY = "age_gate"
AGES = ("adult", "minor")
MINOR_AI_TEXT = "Estudia no usa la IA con menores de 18 años. La agenda, el plan y los recordatorios siguen andando."
AGE_PENDING_TEXT = "Antes de conectar la IA, contestá si tenés 18 años o más."
# Motivos de corte que vuelven solos a una hora conocida (kv "ai_quota_resets_at").
RESETTING_REASONS = ("quota", YOUTUBE_CAP, BUDGET)
NOT_ENOUGH_TEXT = "Todavía no hay suficientes preguntas para un simulacro"

log = logging.getLogger(__name__)

MoodleFactory = Callable[[str, str], Any]
ProviderFactory = Callable[[str], AIProvider]


def ai_age(conn: sqlite3.Connection) -> str | None:
    value = _kv_get(conn, AGE_KEY)
    return value if value in AGES else None


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


def replan_safely(conn: sqlite3.Connection, now: int) -> None:
    """Rehace el plan; si falla, queda en el log y el pedido del usuario sigue."""
    try:
        replan(conn, now)
    except Exception:  # noqa: BLE001 - el plan nunca hace fallar otra cosa
        log.warning("No se pudo rehacer el plan", exc_info=True)


def refresh_plan_throttled(conn: sqlite3.Connection, now: int) -> None:
    """Marca hechas las sesiones según las respuestas y rehace el plan, como mucho cada 10 minutos."""
    try:
        last = _kv_get(conn, PLAN_REFRESH_KEY)
        try:
            last_at = int(last) if last is not None else None
        except ValueError:
            last_at = None  # un valor roto no frena el plan para siempre
        if last_at is not None and 0 <= now - last_at < PLAN_REFRESH_EVERY_S:
            return
        _kv_set(conn, PLAN_REFRESH_KEY, str(now))
        mark_done_from_answers(conn, now)
    except Exception:  # noqa: BLE001
        log.warning("No se pudo actualizar el plan con las respuestas", exc_info=True)
        return
    replan_safely(conn, now)


class Jobs:
    """Sincronización + procesamiento en segundo plano; nunca dos a la vez."""

    def __init__(self, db_path: Path, moodle_client_factory: MoodleFactory,
                 ai_provider_factory: ProviderFactory, clock: Callable[[], float] = time.time):
        self.db_path = db_path
        self.moodle_client_factory = moodle_client_factory
        self.ai_provider_factory = ai_provider_factory
        self.clock = clock
        self._guard = threading.Lock()
        self._running = False
        self._again = False
        # Cuándo reintentar el procesamiento (sin sincronizar), o None.
        self.retry_at: float | None = None
        self._retries = 0  # reintentos seguidos con Gemini saturado

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

    def run_reserved(self, *, sync: bool = True) -> None:
        """Corre lo reservado con `try_start` (y las repeticiones pedidas, que siempre
        sincronizan). Con `sync=False` la primera vuelta solo procesa."""
        try:
            while True:
                self._run_once(sync=sync)
                sync = True
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

    def run_process_now(self) -> bool:
        """Reintenta solo el procesamiento, en este hilo; False si había otra corrida."""
        if not self.try_start():
            return False
        self.run_reserved(sync=False)
        return True

    def ai_allowed(self) -> bool:
        """Solo con la respuesta "adult": menor o sin responder, la IA no se usa."""
        return age_is_adult(self.db_path, timeout_s=BUSY_TIMEOUT_MS / 1000)

    def provider(self, key: str) -> AIProvider:
        """El único camino hacia el proveedor: revisa la puerta de edad en cada llamada."""
        if not self.ai_allowed():
            raise AIBlocked("la IA no está habilitada")
        return GatedProvider(self.ai_provider_factory(key), self.ai_allowed)  # type: ignore[return-value]

    def seconds_until_due(self) -> float:
        """Segundos hasta la próxima sincronización automática (0 si ya toca)."""
        conn = self.connect()
        try:
            last = _kv_get(conn, "last_sync_at")
        finally:
            conn.close()
        if last is None:
            return 0
        return max(0.0, int(last) + SYNC_EVERY_S - self.clock())

    def _run_once(self, *, sync: bool = True) -> None:
        conn = self.connect()
        try:
            if sync:
                self._sync(conn)
            self._process(conn)
            # Material nuevo o exámenes del campus cambian el plan.
            refresh_plan_throttled(conn, int(self.clock()))
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
        self.retry_at = None
        # Menor o sin responder: la clave guardada ni se lee.
        if ai_age(conn) != "adult":
            return
        key = get_secret(GEMINI_KEY)
        # Con la clave rechazada se espera a que el usuario cargue otra.
        if not key or _kv_get(conn, STOP_KEY) == "auth":
            return
        try:
            finished = process_pending(conn, self.provider(key))
        except AIBlocked:
            # Pasó a "menor" a mitad de la corrida: lo que quedó sigue pendiente.
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Falló el procesamiento (%s)", type(exc).__name__)
            return
        self._plan_retry(conn, finished)

    def _plan_retry(self, conn: sqlite3.Connection, finished: int) -> None:
        """Programa el próximo reintento según por qué se cortó el procesamiento.

        - Gemini saturado: espera creciente (5, 10, 20 min, tope 30), que vuelve a
          empezar cuando sale bien un material.
        - Cuota del día: cuando vuelve el primer modelo (kv "ai_quota_resets_at").
        - Tope de YouTube del día: a la medianoche del Pacífico, como la cuota (el mismo kv).
        - Cuota por minuto (sin hora de vuelta) o cualquier otro caso: nada; se espera
          a la próxima sincronización, como siempre.
        """
        if finished:
            self._retries = 0
        reason = _kv_get(conn, STOP_KEY)
        now = self.clock()
        if reason == "unavailable":
            wait = min(RETRY_FIRST_S * 2 ** self._retries, RETRY_MAX_S)
            if wait < RETRY_MAX_S:
                self._retries += 1
            self.retry_at = now + wait
            return
        self._retries = 0
        resets_at = _kv_get(conn, QUOTA_RESET_KEY) if reason in RESETTING_REASONS else None
        if resets_at is not None:
            try:
                # Con un piso: una hora de vuelta ya pasada no reintenta sin parar.
                self.retry_at = max(float(resets_at), now + POLL_S)
            except ValueError:
                pass


def forget_moodle_session(site_id: str) -> None:
    delete_secret(moodle_token_name(site_id))
    delete_secret(moodle_private_name(site_id))
    # La URL del feed lleva un token del campus: se va con la sesión.
    delete_secret(feed_secret_name(site_id))


def periodic_sync(jobs: Jobs, stop: threading.Event, *,
                  sleep: Callable[[float], bool] | None = None) -> None:
    """Sincroniza cada SYNC_EVERY_S mientras la app está abierta (la primera, cuando toca).

    Entre sincronizaciones, si quedó programado un reintento del procesamiento
    (`jobs.retry_at`), lo corre a su hora sin sincronizar. `sleep(segundos)` espera
    y devuelve True si hay que terminar (por defecto, `stop.wait`). La hora es `jobs.clock`.
    """
    sleep = sleep or stop.wait
    next_sync: float | None = None
    while not stop.is_set():
        try:
            now = jobs.clock()
            if next_sync is None:
                next_sync = now + jobs.seconds_until_due()
            if now >= next_sync:
                next_sync = now + SYNC_EVERY_S
                jobs.run_now()
                continue
            retry_at = jobs.retry_at
            if retry_at is not None and now >= retry_at:
                if jobs.run_process_now():
                    continue
                retry_at = None  # hay otra corrida: al terminar, ella reprograma
            wake = next_sync if retry_at is None else min(next_sync, retry_at)
            if sleep(min(wake - now, POLL_S)):
                return
        except Exception as exc:  # noqa: BLE001 - el hilo no debe morir
            log.warning("Falló la sincronización periódica (%s)", type(exc).__name__)
            next_sync = jobs.clock() + SYNC_EVERY_S
            if sleep(POLL_S):
                return


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
    topic_ids: list[int] | None = Field(None, max_length=50)   # varios temas mezclados
    session_id: int | None = None        # sesión del plan: repaso mezclado y después foco
    weakest: bool = False
    size: int = Field(10, ge=1, le=50)


# Modos que se responden desde la práctica (el simulacro tiene su ruta; pretest y probe, la suya).
PRACTICE_MODES = ("mcq", "mcq_think_first", "typed", "typed_then_mcq")


class AnswerIn(BaseModel):
    question_id: int
    chosen_index: StrictInt | None = None   # posición tocada en `shown_order`; None si es escrita
    typed_text: str | None = Field(None, max_length=200)
    # 0 adiviné, 1 me parece, 2 seguro. La forma vieja (sin confianza) se acepta durante una
    # versión y cuenta como "Me parece".
    confidence: StrictInt | None = None
    mode: str = "mcq"
    latency_ms: StrictInt | None = None
    options_revealed_ms: StrictInt | None = None
    shown_order: list[StrictInt] | None = None


class ExplainIn(BaseModel):
    same: bool                              # "¿Dijiste lo mismo?" Sí / No; lo que escribió no se manda


class RecallIn(BaseModel):
    text: str = Field(max_length=5000)      # nunca se guarda ni se loguea


class PretestAnswerIn(BaseModel):
    question_id: int
    chosen_index: StrictInt                 # posición tocada en `shown_order`
    shown_order: list[StrictInt] | None = None


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


class ExamIn(BaseModel):
    course_id: int
    name: str
    at: int
    topic_ids: list[int] = []
    max_grade: float | None = None
    kind: str = "parcial"
    closes_at: int | None = None


class ExamPatchIn(BaseModel):
    """Solo cambian los campos que vienen en el pedido."""
    name: str | None = None
    at: int | None = None
    max_grade: float | None = None
    time_limit_s: int | None = None
    topic_ids: list[int] | None = None
    closes_at: int | None = None


class SlotIn(BaseModel):
    course_id: int
    weekday: int
    start_min: int
    end_min: int
    place: str | None = None


class PlanSettingsIn(BaseModel):
    availability: list[int] | None = None  # minutos por día, de lunes (0) a domingo (6)
    study_start_min: int | None = None


class MockStartIn(BaseModel):
    exam_id: int
    predicted_grade: float | None = None
    short: bool = False   # simulacro corto de 6 preguntas (el sugerido en un recuperatorio)


class MockAnswerIn(BaseModel):
    index: int | None = None
    typed: str | None = Field(None, max_length=200)


MAX_MOCK_ANSWERS = 50   # un simulacro tiene a lo sumo 20 preguntas


class MockFinishIn(BaseModel):
    # Cada respuesta: el índice de la opción (así era antes), {"index": n} o {"typed": "texto"}.
    answers: dict[int, int | MockAnswerIn] = Field(default_factory=dict, max_length=MAX_MOCK_ANSWERS)


class ExamResultIn(BaseModel):
    grade: float
    max_grade: float | None = None


class SettingsIn(BaseModel):
    reminders: bool | None = None
    age: str | None = None
    # Any: se valida a mano para responder 400 (no 422) si no es true/false.
    allow_download: Any = None


def _sort_key(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def _local_midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())


def week_monday(epoch: int) -> date:
    """Lunes local de la semana que contiene `epoch`."""
    day = datetime.fromtimestamp(epoch, TZ).date()
    return day - timedelta(days=day.weekday())


def agenda_week(conn: sqlite3.Connection, monday: date, now: int) -> dict:
    """Los 7 días locales desde `monday` con clases, exámenes, entregas, eventos y sesiones por hora."""
    start = _local_midnight(monday)
    end = _local_midnight(monday + timedelta(days=7))
    items: list[dict] = []
    for o in occurrences(conn, start, end):
        items.append({"kind": "class", "id": o.slot_id, "title": o.course or "Clase", "course": o.course,
                      "starts_at": o.starts_at, "ends_at": o.ends_at, "place": o.place})
    for e in conn.execute(
        "SELECT e.id, e.name, e.opens_at, e.closes_at, COALESCE(e.kind, 'parcial') AS kind, "
        "COALESCE(c.fullname, '') AS course FROM exam e "
        "LEFT JOIN course c ON c.id = e.course_id "
        "WHERE (e.opens_at >= ? AND e.opens_at < ?) OR (e.kind = 'inscripcion' AND e.closes_at >= ? AND e.closes_at < ?)",
        (start, end, start, end),
    ):
        if e["kind"] == ENROLLMENT:
            for phase, at in (("open", e["opens_at"]), ("close", e["closes_at"])):
                if at is not None and start <= at < end:
                    items.append({"kind": "enrollment", "phase": phase, "id": e["id"], "title": e["name"] or "",
                                  "course": e["course"], "starts_at": at})
            continue
        items.append({"kind": "exam", "id": e["id"], "title": e["name"] or "", "course": e["course"],
                      "starts_at": e["opens_at"], "ends_at": e["opens_at"] + EXAM_MIN * 60})
    for d in conn.execute(
        "SELECT d.id, d.kind, d.name, d.due_at, d.url, COALESCE(c.fullname, '') AS course "
        "FROM deadline d LEFT JOIN course c ON c.id = d.course_id WHERE d.due_at >= ? AND d.due_at < ?",
        (start, end),
    ):
        items.append({"kind": "event" if d["kind"] == "event" else "deadline", "id": d["id"],
                      "title": d["name"] or "", "course": d["course"], "starts_at": d["due_at"], "url": d["url"]})
    for p in sessions(conn, start, end):
        # `title` dice qué se hace en la sesión; `session_kind` y `topic_ids`, con qué temas.
        items.append({"kind": "session", "id": p.id, "title": p.label, "course": p.course,
                      "starts_at": p.starts_at, "ends_at": p.starts_at + p.minutes * 60,
                      "topic_id": p.topic_id, "topic": p.topic, "done": p.done,
                      "session_kind": p.kind, "topic_ids": p.topic_ids, "exam_id": p.exam_id,
                      "review_minutes": p.review_minutes, "focus_minutes": p.focus_minutes})

    days = [{"date": (monday + timedelta(days=i)).isoformat(), "items": []} for i in range(7)]
    for item in sorted(items, key=lambda i: i["starts_at"]):
        # Día local: lo que pasa a las 22:00 en Buenos Aires ya es el día siguiente en UTC.
        index = (datetime.fromtimestamp(item["starts_at"], TZ).date() - monday).days
        days[index]["items"].append(item)

    # ¿Llegás?: los exámenes de esta semana o, si no hay, el próximo.
    health = plan_health(conn, now)
    in_week = [h for h in health if start <= h.at < end]
    return {"days": days, "health": in_week or health[:1]}


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
    update_http_factory: Callable[[], httpx.Client] | None = None,
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
        age = ai_age(conn)
        # Un menor no usa la IA: su clave (si había) no cuenta ni se muestra.
        stop_reason = None if age == "minor" else _kv_get(conn, STOP_KEY)
        needs_key = not get_secret(GEMINI_KEY) or stop_reason == "auth"
        if not site_id:
            step = "facultad"
        elif not get_secret(moodle_token_name(site_id)):
            step = "login"
        elif age is None and needs_key:
            step = "edad"
        elif age == "adult" and needs_key:
            step = "ia"
        else:
            step = "listo"
        last = _kv_get(conn, "last_sync_at")
        resets_at = _kv_get(conn, QUOTA_RESET_KEY)
        return {
            "step": step,
            "age": age,
            "site_id": site_id,
            "ai_stop_reason": stop_reason,
            # Con cuota agotada: cuándo vuelve el primer modelo (epoch), si se sabe.
            "ai_quota_resets_at": int(resets_at) if resets_at and stop_reason in RESETTING_REASONS else None,
            "syncing": jobs.running,
            "last_sync_at": int(last) if last else None,
            "last_sync_error": _kv_get(conn, SYNC_ERROR_KEY),
        }

    @app.get("/api/version")
    def version(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # La consulta a GitHub es perezosa (una vez por día) y falla en silencio.
        latest = None
        try:
            with (update_http_factory or default_http)() as http:
                latest = latest_release(
                    http,
                    int(time.time()),
                    lambda key: _kv_get(conn, key),
                    lambda key, value: _kv_set(conn, key, value),
                )
        except Exception:  # noqa: BLE001 - sin aviso, pero la ruta responde igual
            log.info("falló el aviso de versión", exc_info=True)
        return {"current": __version__, "latest": latest}

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
        if get_secret(moodle_token_name(site_id)) != pair.token:
            # Sesión nueva (quizás otra cuenta): el feed de la anterior no se hereda.
            delete_secret(feed_secret_name(site_id))
            _kv_delete(conn, feed_status_key(site_id))
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
        age = ai_age(conn)
        if age == "minor":
            raise HTTPException(403, MINOR_AI_TEXT)
        if age is None:
            raise HTTPException(403, AGE_PENDING_TEXT)
        if body.provider != "gemini":
            raise HTTPException(400, "proveedor no soportado")
        key = body.key.strip()
        if not key:
            raise HTTPException(400, "falta la clave")
        try:
            jobs.provider(key).check_key()
        except AIBlocked:
            # La edad cambió a "menor" justo después de la revisión de arriba.
            raise HTTPException(403, MINOR_AI_TEXT) from None
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

    @app.get("/api/progress")
    def progress(conn: sqlite3.Connection = Depends(get_conn)):
        return progress_view(conn)

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
        # Temas de cada clase y cuántas preguntas se pueden practicar de ellos ("Practicar esta clase").
        topics_of: dict[int, list[int]] = {}
        for r in conn.execute("SELECT material_id, topic_id FROM material_topic ORDER BY topic_id"):
            topics_of.setdefault(r["material_id"], []).append(r["topic_id"])
        questions_in = {
            r["topic_id"]: r["n"]
            for r in conn.execute(
                "SELECT topic_id, COUNT(*) AS n FROM question WHERE verified = 1 AND retired = 0 GROUP BY topic_id"
            )
        }
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
                "topic_ids": topics_of.get(r["id"], []),
                "questions": sum(questions_in.get(t, 0) for t in topics_of.get(r["id"], [])),
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
        if body.session_id is not None:
            try:
                return build_session_quiz(conn, body.session_id, size=body.size, now=now)
            except LookupError:
                raise HTTPException(404, "sesión inexistente") from None
        topic_ids = [body.topic_id] if body.topic_id is not None else body.topic_ids or None
        if topic_ids is not None and len(topic_ids) > 1:
            marks = ",".join("?" * len(set(topic_ids)))
            rows = conn.execute(f"SELECT DISTINCT id, course_id FROM topic WHERE id IN ({marks})",
                                sorted(set(topic_ids))).fetchall()
            if len(rows) < len(set(topic_ids)):
                raise HTTPException(404, "tema inexistente")
            if len({r["course_id"] for r in rows}) > 1:
                raise HTTPException(400, "Elegí temas de una sola materia")
        course_id = _quiz_course(conn, topic_ids[0] if topic_ids else None, now)
        if course_id is None:
            return []
        return build_quiz(conn, course_id, topic_ids=topic_ids, weakest=body.weakest, size=body.size, now=now)

    @app.post("/api/answer")
    def answer(body: AnswerIn, conn: sqlite3.Connection = Depends(get_conn)):
        now = int(time.time())
        if body.mode not in PRACTICE_MODES:
            raise HTTPException(400, f"Modo de respuesta desconocido: {body.mode}")
        try:
            feedback = record_answer(
                conn, body.question_id, body.chosen_index, now,
                confidence=UNSURE if body.confidence is None else body.confidence,
                mode=body.mode, latency_ms=body.latency_ms, option_order=body.shown_order,
                typed_text=body.typed_text, options_revealed_ms=body.options_revealed_ms,
            )
        except LookupError:
            raise HTTPException(404, "pregunta no disponible") from None
        except GradingUnavailable:
            # Worker caído o lento: no es culpa de lo escrito y no se registró nada.
            raise HTTPException(503, "No pude corregir ahora, probá de nuevo") from None
        except ValueError as exc:
            # Incluye la respuesta escrita ilegible ("No pude leer la expresión"): no se registró nada.
            raise HTTPException(400, str(exc)) from None
        refresh_plan_throttled(conn, now)
        return feedback

    @app.post("/api/answers/{answer_id}/explain")
    def answer_explain(answer_id: int, body: ExplainIn, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            record_explain_match(conn, answer_id, body.same)
        except LookupError:
            raise HTTPException(404, "respuesta inexistente") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        refresh_plan_throttled(conn, int(time.time()))
        return {"ok": True}

    # "3 preguntas para arrancar": no toca el repaso ni el plan, así que no se replanifica.
    @app.post("/api/pretest/{material_id}/start")
    def pretest_start(material_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            return build_pretest(conn, material_id)
        except LookupError:
            raise HTTPException(404, "Esta clase no tiene preguntas para arrancar") from None

    @app.post("/api/pretest/answer")
    def pretest_answer(body: PretestAnswerIn, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            return record_pretest_answer(conn, body.question_id, body.chosen_index, int(time.time()),
                                         option_order=body.shown_order)
        except LookupError:
            raise HTTPException(404, "pregunta no disponible") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/pretest/{material_id}/close")
    def pretest_close(material_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            close_pretest(conn, material_id, int(time.time()))
        except LookupError:
            raise HTTPException(404, "La clase no existe") from None
        return {"ok": True}

    # "Contá lo que te acordás": el texto se compara en memoria y no se guarda ni se loguea.
    @app.post("/api/materials/{material_id}/recall")
    def recall(material_id: int, body: RecallIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = conn.execute("SELECT summary_md FROM material WHERE id = ?", (material_id,)).fetchone()
        if row is None or not (row["summary_md"] or "").strip():
            raise HTTPException(404, "Esta clase no tiene resumen")
        return {"headings": [asdict(h) for h in recall_check(row["summary_md"], body.text)]}

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

    # --- Agenda, exámenes, horarios, plan, simulacro y calendario (datos locales) ----------------

    @app.get("/api/agenda")
    def agenda(week_start: int | None = None, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        now = int(time.time())
        try:
            # Cualquier momento de la semana se lleva a su lunes 00:00 local.
            monday = week_monday(now if week_start is None else week_start)
        except (OverflowError, OSError, ValueError):
            raise HTTPException(400, "Semana inválida") from None
        return agenda_week(conn, monday, now)

    @app.get("/api/plan/health")
    def health(conn: sqlite3.Connection = Depends(get_conn)):
        return plan_health(conn, int(time.time()))

    @app.get("/api/courses")
    def courses(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        rows = [{"id": r["id"], "name": r["name"] or ""}
                for r in conn.execute("SELECT id, MIN(fullname) AS name FROM course GROUP BY id")]
        # Orden alfabético sin que los acentos manden ("Álgebra" antes que "Física").
        return sorted(rows, key=lambda c: (_sort_key(c["name"]), c["id"]))

    def exam_exists(conn: sqlite3.Connection, exam_id: int) -> None:
        if conn.execute("SELECT 1 FROM exam WHERE id = ?", (exam_id,)).fetchone() is None:
            raise HTTPException(404, "El examen no existe")

    @app.get("/api/exams")
    def exams(conn: sqlite3.Connection = Depends(get_conn)):
        return list_exams(conn, int(time.time()))

    @app.post("/api/exams")
    def create_exam(body: ExamIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            exam_id = create_manual_exam(conn, body.course_id, body.name, body.at, body.topic_ids,
                                         body.max_grade, kind=body.kind, closes_at=body.closes_at,
                                         now=int(time.time()))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        replan_safely(conn, int(time.time()))
        return {"id": exam_id}

    @app.put("/api/exams/{exam_id}")
    def edit_exam(exam_id: int, body: ExamPatchIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        exam_exists(conn, exam_id)
        fields = body.model_dump(exclude_unset=True)
        if "topic_ids" in fields and fields["topic_ids"] is None:
            fields["topic_ids"] = []  # sin lista: todos los temas del curso
        try:
            update_manual_exam(conn, exam_id, **fields)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        replan_safely(conn, int(time.time()))
        return {"ok": True}

    @app.post("/api/exams/{exam_id}/result")
    def exam_result(exam_id: int, body: ExamResultIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            return set_exam_result(conn, exam_id, body.grade, int(time.time()), body.max_grade)
        except LookupError:
            raise HTTPException(404, "El examen no existe") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/exams/{exam_id}/result/skip")
    def exam_result_skip(exam_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            skip_exam_result(conn, exam_id, int(time.time()))
        except LookupError:
            raise HTTPException(404, "El examen no existe") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True}

    @app.delete("/api/exams/{exam_id}")
    def remove_exam(exam_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        exam_exists(conn, exam_id)
        try:
            delete_manual_exam(conn, exam_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        replan_safely(conn, int(time.time()))
        return {"ok": True}

    @app.get("/api/exams/{exam_id}/topics")
    def topics_of_exam(exam_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        exam_exists(conn, exam_id)
        ids = exam_topics(conn, exam_id)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        names = {r["id"]: r["name"] for r in conn.execute(f"SELECT id, name FROM topic WHERE id IN ({marks})", ids)}
        return [{"id": tid, "name": names[tid]} for tid in ids if tid in names]

    def slot_exists(conn: sqlite3.Connection, slot_id: int) -> None:
        if conn.execute("SELECT 1 FROM class_slot WHERE id = ?", (slot_id,)).fetchone() is None:
            raise HTTPException(404, "El horario no existe")

    @app.get("/api/schedule")
    def schedule(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {"slots": list_slots(conn), "suggestions": suggestions(conn)}

    @app.post("/api/schedule")
    def create_slot(body: SlotIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            slot_id = add_slot(conn, body.course_id, body.weekday, body.start_min, body.end_min, body.place)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        replan_safely(conn, int(time.time()))
        return {"id": slot_id}

    @app.post("/api/schedule/suggestions/{course_id}/dismiss")
    def dismiss_schedule_suggestion(course_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if conn.execute("SELECT 1 FROM course WHERE id = ?", (course_id,)).fetchone() is None:
            raise HTTPException(404, "El curso no existe")
        dismiss_suggestion(conn, course_id)
        return {"ok": True}

    @app.put("/api/schedule/{slot_id}")
    def edit_slot(slot_id: int, body: SlotIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        slot_exists(conn, slot_id)
        try:
            update_slot(conn, slot_id, body.course_id, body.weekday, body.start_min, body.end_min, body.place)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        replan_safely(conn, int(time.time()))
        return {"ok": True}

    @app.delete("/api/schedule/{slot_id}")
    def remove_slot(slot_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        slot_exists(conn, slot_id)
        delete_slot(conn, slot_id)
        replan_safely(conn, int(time.time()))
        return {"ok": True}

    @app.get("/api/plan/settings")
    def plan_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        availability = get_availability(conn)
        return {"availability": [availability[d] for d in range(7)],
                "study_start_min": preferred_start_min(conn)}

    @app.put("/api/plan/settings")
    def save_plan_settings(body: PlanSettingsIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # Todo se valida antes de escribir: un pedido inválido no deja nada a medias.
        if body.availability is not None and len(body.availability) != 7:
            raise HTTPException(400, "La disponibilidad tiene que traer los 7 días")
        if body.study_start_min is not None and not 0 <= body.study_start_min < 1440:
            raise HTTPException(400, "La hora preferida tiene que estar entre 0 y 1439 minutos")
        if body.availability is not None:
            try:
                set_availability(conn, dict(enumerate(body.availability)))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        if body.study_start_min is not None:
            _kv_set(conn, STUDY_START_KEY, str(body.study_start_min))
        replan_safely(conn, int(time.time()))
        return {"ok": True}

    @app.post("/api/mock/start")
    def mock_start(body: MockStartIn, conn: sqlite3.Connection = Depends(get_conn)):
        # NotEnoughQuestions hereda de ValueError: va primero.
        try:
            return start_mock(conn, body.exam_id, int(time.time()), body.predicted_grade, short=body.short)
        except NotEnoughQuestions:
            raise HTTPException(409, SHORT_NOT_ENOUGH_TEXT if body.short else NOT_ENOUGH_TEXT) from None
        except LookupError:
            raise HTTPException(404, "El examen no existe") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/mock/{attempt_id}/finish")
    def mock_finish(attempt_id: int, body: MockFinishIn, conn: sqlite3.Connection = Depends(get_conn)):
        now = int(time.time())
        try:
            answers = {
                qid: ans.model_dump(exclude_none=True) if isinstance(ans, MockAnswerIn) else ans
                for qid, ans in body.answers.items()
            }
            result = finish_mock(conn, attempt_id, answers, now)
        except NotEnoughQuestions:
            raise HTTPException(409, NOT_ENOUGH_TEXT) from None
        except LookupError:
            raise HTTPException(404, "El simulacro no existe") from None
        except GradingUnavailable:
            # Worker caído o lento: no se entregó nada, se puede reintentar.
            raise HTTPException(503, "No pude corregir ahora, probá de nuevo") from None
        except ValueError as exc:
            # finish_mock deshace todo al fallar: si quedó entregado, es porque ya lo estaba.
            row = conn.execute("SELECT finished_at FROM mock_attempt WHERE id = ?", (attempt_id,)).fetchone()
            if row is not None and row["finished_at"] is not None:
                raise HTTPException(409, "Este simulacro ya terminó") from None
            raise HTTPException(400, str(exc)) from None
        refresh_plan_throttled(conn, now)
        return result

    @app.get("/api/mock/{attempt_id}")
    def mock_read(attempt_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        """El resultado de un simulacro ya entregado (si una entrega se repite, se lee de acá)."""
        try:
            return mock_result(conn, attempt_id)
        except LookupError:
            raise HTTPException(404, "El simulacro no existe") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/calendar.ics")
    def calendar(conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        return Response(
            build_ics(conn, int(time.time())),
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="estudia.ics"'},
        )

    @app.get("/api/export/anki.apkg")
    def export_anki(course_id: int | None = None, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            data = build_apkg(conn, course_id)
        except LookupError:
            raise HTTPException(404, "No hay preguntas para exportar") from None
        name = None
        if course_id is not None:
            row = conn.execute("SELECT MIN(fullname) AS n FROM course WHERE id = ?", (course_id,)).fetchone()
            name = row["n"]
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="estudia-{course_slug(name or "todo")}.apkg"'},
        )

    @app.get("/api/export/anki-semana.apkg")
    def export_anki_week(conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        now = int(time.time())
        try:
            data = build_apkg(conn, None, weekly_question_ids(conn, now))
        except LookupError:
            raise HTTPException(404, "No hay preguntas para esta semana") from None
        day = datetime.fromtimestamp(now, TZ).date().isoformat()
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="estudia-semana-{day}.apkg"'},
        )

    # --- Feed del calendario del campus ------------------------------------------------------
    # La URL lleva un token del campus: vive solo en el keyring y sale únicamente por
    # POST /api/calendar-feed/reveal (POST: pasa por OriginGuard).

    def feed_status(conn: sqlite3.Connection) -> str:
        """Estado del feed del campus elegido (el kv global viejo ya no se lee)."""
        site_id = _kv_get(conn, SITE_KEY)
        if not site_id:
            return "unknown"
        status = _kv_get(conn, feed_status_key(site_id))
        # Guardado pero sin secreto (se cerró la sesión): hay que pedirlo de nuevo.
        if status == "saved" and not get_secret(feed_secret_name(site_id)):
            return "unknown"
        return status if status in ("saved", "unavailable") else "unknown"

    @app.get("/api/calendar-feed")
    def calendar_feed(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        site_url = _campus_url(conn)
        return {
            "status": feed_status(conn),
            "campus_calendar_url": campus_calendar_url(site_url) if site_url else None,
        }

    @app.post("/api/calendar-feed/fetch")
    def fetch_calendar_feed(conn: sqlite3.Connection = Depends(require_session)) -> dict:
        site_id = _kv_get(conn, SITE_KEY)
        token = get_secret(moodle_token_name(site_id))
        client = jobs.moodle_client_factory(get_site(site_id).url, token)
        # Nada de este camino toca la sesión del campus.
        try:
            url = fetch_feed_url(client)
        except FeedUnreachable:
            # Sin respuesta del campus: lo guardado queda como está y se reintenta después.
            return {"status": feed_status(conn), "retry": True}
        if url is None:
            delete_secret(feed_secret_name(site_id))
            status = "unavailable"
        else:
            set_secret(feed_secret_name(site_id), url)
            status = "saved"
        _kv_set(conn, feed_status_key(site_id), status)
        return {"status": status, "retry": False}

    @app.post("/api/calendar-feed/reveal")
    def reveal_calendar_feed(conn: sqlite3.Connection = Depends(get_conn)) -> JSONResponse:
        site_id = _kv_get(conn, SITE_KEY)
        url = get_secret(feed_secret_name(site_id)) if site_id else None
        if not url:
            raise HTTPException(404, "no hay feed del calendario guardado")
        return JSONResponse({"url": url}, headers={"Cache-Control": "no-store"})

    @app.get("/api/settings")
    def settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {"reminders": reminders_enabled(conn), "age": ai_age(conn),
                "allow_download": download_allowed(conn)}

    @app.put("/api/settings")
    def save_settings(body: SettingsIn, background: BackgroundTasks,
                      conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if body.reminders is None and body.age is None and body.allow_download is None:
            raise HTTPException(400, "no hay nada que guardar")
        if body.age is not None and body.age not in AGES:
            raise HTTPException(400, "la edad tiene que ser adult o minor")
        if body.allow_download is not None and not isinstance(body.allow_download, bool):
            raise HTTPException(400, "allow_download tiene que ser true o false")
        if body.allow_download is not None:
            _kv_set(conn, ALLOW_DOWNLOAD_KEY, "1" if body.allow_download else "0")
            # Con la descarga prendida, los videos que pedían un enlace público se
            # pueden preparar: vuelven a la cola.
            if body.allow_download and retry_needs_link(conn):
                start_jobs(background)
        if body.reminders is not None:
            _kv_set(conn, REMINDERS_KEY, "1" if body.reminders else "0")
        if body.age is not None:
            _kv_set(conn, AGE_KEY, body.age)
            if body.age == "adult":
                # Responder reanuda el procesamiento que estaba en pausa.
                start_jobs(background)
        return {"ok": True}

    dist = web_dist if web_dist is not None else web_dist_dir()
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    else:
        @app.get("/", response_class=PlainTextResponse)
        def placeholder() -> str:
            return "Estudia está andando. Falta compilar la interfaz (web/dist)."

    return app
