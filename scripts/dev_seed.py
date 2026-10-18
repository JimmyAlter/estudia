"""Levanta Estudia con datos de ejemplo, sin tocar los datos ni el keyring reales.

Uso:
    uv run python scripts/dev_seed.py --port 8780            # app lista, con examen
    uv run python scripts/dev_seed.py --step facultad        # arranca en el onboarding
    uv run python scripts/dev_seed.py --step vencida         # la sesión del campus venció
    uv run python scripts/dev_seed.py --no-exam              # sin exámenes próximos

La base va a una carpeta temporal y los secretos a un keyring en memoria. El
campus y Gemini son dobles: "Actualizar" no sale a internet, y procesar una
clase pendiente termina en el aviso de límite de Gemini.

Con examen (lo normal) también siembra la agenda y el plan, para ver todas las pantallas:
horarios de cursada (y una sugerencia de horario), entregas y un evento, un examen cargado a
mano en 5 días (sesiones de repaso mezclado), un examen pasado sin nota ("¿Cómo te fue?"),
disponibilidad, sesiones del plan (de foco y mezcladas) y un simulacro terminado.

Para lo de la etapa 3a: una clase recién preparada que se ofrece como "3 preguntas para arrancar",
resúmenes con títulos ("Contá lo que te acordás"), 26 respuestas con confianza ("Cómo vengo") en
las que ningún acierto con "Me parece" pidió "Explicá por qué" (así el próximo lo pide), y con
examen, uno ya rendido con simulacro, nota prevista y nota real.

Para "En el celular" (Agenda): el enlace del calendario del campus guardado (de mentira, en el
keyring en memoria) y preguntas que vencen en la semana, así el mazo de la semana sale. Con
examen, además, una inscripción abierta desde ayer que cierra en 2 días.

Para recuperatorio y final (con examen): un recuperatorio cargado hace 3 días, con el simulacro
corto sugerido para hoy, y un final en 20 días (menos de 4 semanas). "Armar el recuperatorio"
aparece en el parcial ya rendido con nota, en la semana anterior de la Agenda. El tema
"Ecuación de la recta" está firme (P ≥ 0,7 el día del recuperatorio): el recuperatorio no lo programa.

Para la etapa 3b (Materiales, en "POLINOMIOS Y SISTEMAS DE ECUACIONES LINEALES"): un apunte en
PDF partido en tres tramos (el primero preparado, con resumen y la página en cada título), unas
diapositivas preparadas, y archivos del campus en cada estado: uno bajándose, uno demasiado
grande, uno que no se pudo leer, uno que no se pudo bajar ("Reintentar") y un .docx que
no se puede leer todavía. Dos preguntas de esos documentos citan la página ("Apunte 3,
pág. 14", enlazada a la página del campus): salen con "Practicar esta clase" y en la revisión
del simulacro de siempre de "Final de Matemática".

Para el simulacro con el formato de la cátedra (Simulacro, con examen): "Parcialito de rectas" tiene
dos parciales viejos cargados (uno leído y otro que se está leyendo) y su formato guardado, con
las preguntas listas (cada una muestra sus puntos). "Final de Matemática" tiene cuatro parciales:
dos leídos (el formato propuesto es su promedio, con un tema que no está en la materia), uno
leyéndose y uno que no se pudo leer; y un formato guardado al que le faltan 2 preguntas de
desarrollo: ofrece el simulacro de siempre. Con un formato guardado, los parciales y el editor
van plegados en "Cambiar el formato o cargar parciales".

La carpeta de datos (parciales y apuntes) también es temporal: nada se escribe en la de verdad.

Con la app lista, el curso tiene 12 preguntas verificadas y una clase pendiente ("Ya hay 12
preguntas…"), y el procesamiento quedó frenado por el tope del día (`ai_stop_reason = "budget"`).
La pantalla de cada cosa está en el informe de la Tarea 19 (`.superpowers/sdd/2026-10-18-etapa3a/`).
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import keyring
import uvicorn
from keyring.backend import KeyringBackend

from estudia.ai.base import AIQuotaError
from estudia.config import DATA_DIR_ENV, data_dir
from estudia.db import connect
from estudia.generation.pipeline import BUDGET, STOP_KEY
from estudia.moodle.calendar_feed import export_url, feed_secret_name, feed_status_key
from estudia.secrets import set_secret
from estudia.server.app import GEMINI_KEY, HOST, SITE_KEY, create_app, moodle_token_name
from estudia.study.exams import ENROLLMENT, FINAL, RETAKE, create_manual_exam, set_exam_result
from estudia.study.mock_exam import finish_mock, start_mock
from estudia.study.past_exams import file_path as past_exam_file
from estudia.study.planner import replan, set_availability
from estudia.study.schedule import add_slot

SITE_ID = "utn-frba"
SITE_URL = "https://aulasvirtuales.frba.utn.edu.ar"
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAY = 86400


def local_at(now: int, days: int, hour: int, minute: int = 0) -> int:
    """Epoch de las hour:minute locales, `days` días después de hoy: horas redondas en la Agenda."""
    day = datetime.fromtimestamp(now, TZ).date() + timedelta(days=days)
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).timestamp())


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


class OfflineMoodle:
    """Campus de mentira: responde sin cursos, así la sincronización no cambia nada."""

    def __init__(self, site_url: str, token: str):
        self.site_url = site_url

    def call(self, fn, **params):
        if fn == "core_webservice_get_site_info":
            return {"userid": 1, "fullname": "Estudiante de prueba"}
        return []


class QuotaProvider:
    """Gemini de mentira: acepta cualquier clave y siempre está sin cuota para procesar."""

    youtube_url_denied = False
    youtube_url_denied_until = 0.0
    allow_download = False
    key_id = "dev"
    last_analysis_model = None
    last_video_seconds = None

    def __init__(self):
        # Lo que la cola lee y escribe en cada corrida (cuota, modelos agotados, videos sin enlace).
        self.requests: dict[str, int] = {}
        self.exhausted_until: dict[str, float] = {}
        self.url_denied_videos: set[str] = set()

    def analyze_video(self, youtube_id, title):
        raise AIQuotaError("cuota agotada (simulado)")

    # Documentos, parciales y preguntas con el formato: también sin cuota (el parcial sigue "Leyendo").
    def analyze_document(self, doc):
        raise AIQuotaError("cuota agotada (simulado)")

    def extract_exam_style(self, exam, course_topics):
        raise AIQuotaError("cuota agotada (simulado)")

    def generate_style_questions(self, request):
        raise AIQuotaError("cuota agotada (simulado)")

    def answer_blind(self, stem, options):
        return 1


SUMMARY_1 = """# Rectas
La ecuación explícita de la recta es y = mx + b, donde m es la pendiente y b la ordenada al origen.
## Pendiente
- La pendiente mide cuánto sube y por cada unidad que avanza x: m = (y₂ − y₁) / (x₂ − x₁).
- Si m > 0 la recta crece; si m < 0, decrece; si m = 0, es horizontal.
## Ordenada al origen
Es el valor de y cuando x = 0: el punto (0, b) donde la recta corta al eje y.
## Recta que pasa por un punto
Con un punto (x₀, y₀) y la pendiente m: y − y₀ = m(x − x₀). Despejando se obtiene **b = y₀ − m·x₀**.
"""

SUMMARY_2 = """# Paralelismo y perpendicularidad
- Dos rectas son paralelas si tienen la misma pendiente: m₁ = m₂.
- Son perpendiculares si el producto de sus pendientes es −1: m₁ · m₂ = −1.
Ejemplo: y = 2x + 1 e y = −½x + 3 son perpendiculares, porque 2 · (−½) = −1.
"""

QUESTIONS = [
    # (material, tema, enunciado, opciones, correcta, explicación, error común, segundo)
    (1, 1, "La recta de pendiente 3 que pasa por (2, 1) corta al eje y en…", ["1", "7", "−5", "3"], 2,
     "y = 3x + b. Como (2, 1) está en la recta: 1 = 3·2 + b, entonces b = 1 − 6 = −5. El corte con el eje y es (0, b).",
     "Sumar en vez de restar al despejar b.", 1060),
    (1, 1, "¿Cuál es la pendiente de la recta que pasa por (1, 2) y (3, 8)?", ["2", "3", "6", "1/3"], 1,
     "m = (y₂ − y₁) / (x₂ − x₁) = (8 − 2) / (3 − 1) = 6 / 2 = 3.",
     "Invertir el cociente y calcular (x₂ − x₁) / (y₂ − y₁).", 412),
    (1, 1, "Si f(x) = −2x + 4, ¿cuánto vale f(3)?", ["−2", "10", "2", "−10"], 0,
     "f(3) = −2·3 + 4 = −6 + 4 = −2.", "Olvidar el signo de la pendiente: 2·3 + 4 = 10.", 655),
    (2, 2, "¿Qué pendiente tiene una recta perpendicular a y = 2x + 1?", ["2", "−2", "1/2", "−1/2"], 3,
     "Las pendientes de rectas perpendiculares cumplen m₁ · m₂ = −1. Con m₁ = 2: m₂ = −1/2.",
     "Tomar solo el inverso (1/2) y olvidarse de cambiar el signo.", 300),
    (2, 2, "Las rectas y = 4x − 1 e y = 4x + 7 son…", ["paralelas", "perpendiculares", "la misma recta", "secantes no perpendiculares"], 0,
     "Tienen la misma pendiente (m = 4) y distinta ordenada al origen, así que son paralelas y distintas.",
     "Confundir ordenadas distintas con rectas que se cortan.", 145),
    (2, 2, "¿Para qué valor de k la recta y = kx + 2 es paralela a y = −3x?", ["3", "−3", "1/3", "−1/3"], 1,
     "Para ser paralelas las pendientes deben ser iguales: k = −3.",
     "Usar la condición de perpendicularidad en lugar de la de paralelismo.", 210),
]


SUMMARY_5 = """# Distancia y punto medio
## Distancia entre dos puntos
La distancia entre (x₁, y₁) y (x₂, y₂) es d = √((x₂ − x₁)² + (y₂ − y₁)²): sale del teorema de Pitágoras.
## Punto medio
El punto medio de un segmento promedia las coordenadas: M = ((x₁ + x₂)/2, (y₁ + y₂)/2).
## Circunferencia
Los puntos a distancia r de un centro (h, k) cumplen (x − h)² + (y − k)² = r².
"""

# La clase recién preparada que se ofrece como "3 preguntas para arrancar" (sin respuestas).
PRETEST_QUESTIONS = [
    (7, "¿Cuál es la distancia entre (0, 0) y (3, 4)?", ["5", "7", "1", "25"], 0,
     "d = √((3 − 0)² + (4 − 0)²) = √(9 + 16) = √25 = 5.", "Sumar las diferencias sin elevar al cuadrado: 3 + 4 = 7.", 240),
    (8, "¿Cuál es el punto medio entre (2, 6) y (4, 10)?", ["(6, 16)", "(3, 8)", "(1, 2)", "(2, 4)"], 1,
     "Se promedian las coordenadas: ((2 + 4)/2, (6 + 10)/2) = (3, 8).", "Sumar las coordenadas sin dividir por 2.", 610),
    (9, "¿Qué radio tiene la circunferencia (x − 1)² + (y + 2)² = 9?", ["9", "−3", "3", "81"], 2,
     "La forma es (x − h)² + (y − k)² = r², así que r² = 9 y r = 3.", "Tomar el 9 como radio en vez de r².", 1130),
]

# Respuestas de práctica con confianza, de más vieja a más nueva: (pregunta, correcta, confianza).
# 2 = Seguro (10 de 14, 71 %), 1 = Me parece (5 de 8), 0 = Adiviné (1 de 4). Ninguna pidió
# "Explicá por qué": el próximo acierto con "Me parece" en la práctica lo pide.
CONFIDENCE_ANSWERS = (
    [(q, 1, 2) for q in (1, 2, 3, 5, 6, 1, 2, 3, 5, 6)] + [(q, 0, 2) for q in (4, 4, 6, 1)]
    + [(q, 1, 1) for q in (2, 3, 5, 6, 1)] + [(q, 0, 1) for q in (4, 6, 2)]
    + [(3, 1, 0)] + [(q, 0, 0) for q in (4, 5, 6)]
)


def seed(db_path: Path, *, with_exam: bool) -> None:
    now = int(time.time())
    conn = connect(db_path)
    conn.execute("INSERT INTO site VALUES (?, ?, 1, 'Estudiante de prueba')",
                 (SITE_ID, SITE_URL))
    conn.execute("INSERT INTO course VALUES (101, ?, 'Matemática', 'AM1')", (SITE_ID,))
    conn.executemany("INSERT INTO section VALUES (?, 101, ?, ?, ?)", [
        (10, 2, "NÚMEROS REALES, FUNCIÓN LINEAL Y CUADRÁTICA", 1),
        (11, 3, "POLINOMIOS Y SISTEMAS DE ECUACIONES LINEALES", 1),
        (12, 4, "TRIGONOMETRÍA", 0),
    ])
    conn.executemany(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status, summary_md, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 10, 501, "youtube", "Video-clase 1: Rectas.", "dQw4w9WgXcQ", "done", SUMMARY_1, now),
            (2, 10, 502, "youtube", "Video-clase 2: Paralelismo y perpendicularidad de rectas.", "M7lc1UVf-VE", "done", SUMMARY_2, now),
            (3, 10, 503, "youtube", "Video-clase 3: Funciones cuadráticas.", "aqz-KE-bpKQ", "pending", None, now),
            (4, 11, 504, "page", "Guía de ejercicios: polinomios", None, "unsupported", None, now),
        ],
    )
    conn.executemany("INSERT INTO topic VALUES (?, 101, ?, ?)", [
        (1, "funciones-lineales", "Funciones lineales y pendiente"),
        (2, "paralelismo", "Paralelismo y perpendicularidad"),
        (3, "cuadraticas", "Funciones cuadráticas"),
    ])
    conn.executemany("INSERT INTO material_topic VALUES (?, ?)", [(1, 1), (2, 2), (3, 3)])
    # Con check_expr se pueden responder escribiendo (la 2 ya se acertó: sale como escrita).
    checks = {2: "3", 3: "-2"}
    for i, (mat, topic, stem, options, correct, expl, err, sec) in enumerate(QUESTIONS, start=1):
        conn.execute(
            "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
            " explanation, common_error, source_seconds, check_expr, verified, dedupe_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (i, mat, topic, stem, json.dumps(options, ensure_ascii=False), correct, expl, err, sec,
             checks.get(i), f"q{i}"),
        )
    # Algo de práctica previa: tema 1 bastante bien, tema 2 flojo.
    conn.executemany("INSERT INTO answer(question_id, chosen_index, correct, at) VALUES (?, ?, ?, ?)", [
        (1, 2, 1, now - 2 * DAY), (2, 1, 1, now - 2 * DAY), (3, 0, 1, now - DAY), (3, 1, 0, now - 3 * DAY),
        (4, 2, 0, now - DAY), (5, 0, 1, now - DAY),
    ])
    for n, (qid, correct, confidence) in enumerate(CONFIDENCE_ANSWERS):
        conn.execute(
            "INSERT INTO answer(question_id, chosen_index, correct, at, confidence, mode, explain_prompted)"
            " VALUES (?, ?, ?, ?, ?, 'mcq', 0)",
            (qid, QUESTIONS[qid - 1][4] if correct else (QUESTIONS[qid - 1][4] + 1) % 4, correct,
             now - 6 * DAY + n * 3600, confidence),
        )
    # Un tema bien sabido (una pregunta acertada cuatro veces, espaciadas): su P el día del
    # recuperatorio queda en 0,7 o más, así que el recuperatorio no lo programa. Con esta, el
    # curso llega a 10 preguntas verificadas (con la clase 3 pendiente: "Ya hay 10 preguntas").
    conn.execute("INSERT INTO topic VALUES (5, 101, 'ecuacion-recta', 'Ecuación de la recta')")
    conn.execute("INSERT INTO material_topic VALUES (1, 5)")
    # Correcta en el índice 0: el simulacro sembrado la contesta bien (ver `seed_agenda`).
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
        " explanation, common_error, source_seconds, verified, dedupe_key)"
        " VALUES (10, 1, 5, ?, ?, 0, ?, ?, 1180, 1, 'q10')",
        ("¿Qué ecuación tiene la recta de pendiente 2 que pasa por (0, 3)?",
         json.dumps(["y = 2x + 3", "y = 3x + 2", "y = 2x − 3", "y = −2x + 3"], ensure_ascii=False),
         "Con pendiente m = 2 y ordenada al origen b = 3 (el punto (0, 3)): y = 2x + 3.",
         "Cambiar de lugar la pendiente y la ordenada al origen."),
    )
    conn.executemany("INSERT INTO answer(question_id, chosen_index, correct, at) VALUES (10, 0, 1, ?)",
                     [(now - days * DAY,) for days in (40, 25, 12, 4)])
    if with_exam:
        opens = local_at(now, 21, 8)
        conn.execute("INSERT INTO exam(id, course_id, cmid, name, opens_at, closes_at, attempts, time_limit_s, "
                     "max_grade) VALUES (1, 101, 600, 'PRIMER CUESTIONARIO', ?, ?, 1, 7200, 12)",
                     (opens, opens + 7 * DAY))
    conn.execute("INSERT INTO kv VALUES ('last_sync_at', ?)", (str(now - 2 * 3600),))
    # El tope de pedidos del día frenó la clase pendiente: Inicio lo dice con calma ("Lo de hoy ya está…").
    conn.execute("INSERT INTO kv VALUES (?, ?)", (STOP_KEY, BUDGET))
    conn.commit()
    conn.close()
    if with_exam:
        seed_agenda(db_path, now)
        seed_exam_styles(db_path, now)
    # Después del plan y los simulacros, para que ninguno use sus preguntas (la oferta pide cero respuestas).
    seed_pretest(db_path, now)
    seed_documents(db_path, now)


def _style(groups, minutes, topic_ids, *, formula_sheet=False, unmatched=()):
    return json.dumps({
        "groups": [{"kind": k, "count": c, "points_each": p} for k, c, p in groups],
        "minutes": minutes, "max_grade": 10, "topic_ids": list(topic_ids),
        "topics_unmatched": list(unmatched), "formula_sheet": formula_sheet,
    }, ensure_ascii=False)


def seed_exam_styles(db_path: Path, now: int) -> None:
    """Parciales viejos y formatos de la cátedra (ver el docstring del módulo)."""
    conn = connect(db_path)
    exam_id = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM exam")}
    rectas, final = exam_id["Parcialito de rectas"], exam_id["Final de Matemática"]
    ready = _style([("opcion_multiple", 5, 2)], 60, [1, 2])
    past = [
        # (id, examen, nombre, tipo, estado, formato leído)
        (1, rectas, "Parcialito 2024, primer cuatrimestre.pdf", "pdf", "done", ready),
        (2, rectas, "Foto parcialito 2023.jpg", "jpeg", "pending", None),
        (3, final, "Final diciembre 2024.pdf", "pdf", "done",
         _style([("opcion_multiple", 4, 1), ("desarrollo", 2, 3)], 120, [1, 2], formula_sheet=True)),
        (4, final, "Final julio 2025.png", "png", "done",
         _style([("opcion_multiple", 6, 1), ("desarrollo", 2, 2)], 150, [1, 2, 5], unmatched=["Cónicas"])),
        (5, final, "Final febrero 2025.pdf", "pdf", "pending", None),
        (6, final, "Foto final marzo 2024.jpg", "jpeg", "failed", None),
    ]
    for pid, exam, name, kind, status, style in past:
        path = past_exam_file(data_dir(), pid, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"parcial de prueba {pid}".encode()
        path.write_bytes((b"%PDF-1.4\n" if kind == "pdf" else b"") + content)
        conn.execute(
            "INSERT INTO past_exam(id, exam_id, name, kind, path, size, sha256, status, style_json, error,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, exam, name, kind, f"parciales/{path.name}", len(content), f"sha-{pid}", status, style,
             "No se pudo leer" if status == "failed" else None, now - (7 - pid) * 3600),
        )
    # Guardados: el del parcialito tiene sus preguntas; al del final le faltan las de desarrollo.
    conn.execute("UPDATE exam SET style_json = ? WHERE id = ?", (ready, rectas))
    conn.execute("UPDATE exam SET style_json = ? WHERE id = ?", (
        _style([("opcion_multiple", 5, 1), ("desarrollo", 2, 2.5)], 135, [1, 2], formula_sheet=True,
               unmatched=["Cónicas"]), final))
    conn.commit()
    conn.close()


SUMMARY_PART_1 = """# Polinomios y raíces
## Grado y coeficiente principal (pág. 3)
El grado es el mayor exponente de x con coeficiente distinto de cero; ese coeficiente es el principal.
## Teorema del resto (pág. 14)
El resto de dividir P(x) por (x − a) es P(a). Si P(a) = 0, entonces a es raíz y (x − a) divide a P.
## Regla de Ruffini (pág. 22)
- Divide por (x − a) usando solo los coeficientes.
- El último número que queda es el resto.
"""

# (id, material, enunciado, opciones, correcta, explicación, error común, página)
DOCUMENT_QUESTIONS = [
    (11, 21, "¿Cuál es el resto de dividir P(x) = x³ − 2x + 1 por (x − 2)?", ["5", "1", "−3", "0"], 0,
     "Por el teorema del resto, el resto es P(2) = 8 − 4 + 1 = 5.",
     "Evaluar en −2 en lugar de 2.", 14),
    (12, 24, "¿Cuántas soluciones tiene un sistema lineal con dos rectas paralelas distintas?",
     ["Ninguna", "Una", "Dos", "Infinitas"], 0,
     "Dos rectas paralelas distintas no se cortan: el sistema es incompatible.",
     "Confundir paralelas distintas con la misma recta (infinitas soluciones).", 5),
]


def seed_documents(db_path: Path, now: int) -> None:
    """Archivos del campus en Materiales: un PDF en tramos, diapositivas y cada estado de un archivo.

    Preparados hace 10 días, así no compiten con la clase de "3 preguntas para arrancar".
    """
    from estudia.material.files import (DOWNLOAD_FAILED_TEXT, FAIL_DOWNLOAD, NOT_READABLE_YET,
                                        TOO_LARGE_TEXT, UNREADABLE_TEXT)

    at = now - 10 * DAY
    pluginfile = f"{SITE_URL}/webservice/pluginfile.php"
    conn = connect(db_path)
    rows = [
        # (id, kind, title, status, summary, error, module_id, parent, from, to, pages, fail_kind, remote_url)
        (20, "pdf", "Apunte 3", "split", None, None, 701, None, None, None, 96, None,
         f"{pluginfile}/31/mod_resource/content/1/apunte3.pdf"),
        (21, "pdf", "Capítulo 1: Polinomios y raíces", "done", SUMMARY_PART_1, None, None, 20, 1, 40, None, None, None),
        (22, "pdf", "Apunte 3, págs. 41–80", "pending", None, None, None, 20, 41, 80, None, None, None),
        (23, "pdf", "Apunte 3, págs. 81–96", "pending", None, None, None, 20, 81, 96, None, None, None),
        (24, "slides", "Clase 5: Sistemas de ecuaciones", "done", None, None, 702, None, None, None, 18, None, None),
        (25, "pdf", "Guía de ejercicios 2", "downloading", None, None, 703, None, None, None, None, None,
         f"{pluginfile}/33/mod_resource/content/1/guia2.pdf"),
        (26, "pdf", "Apunte completo escaneado", "too_large", None, TOO_LARGE_TEXT, 704, None, None, None, None,
         None, None),
        (27, "slides", "Presentación de la cátedra", "unreadable", None, UNREADABLE_TEXT, 705, None, None, None,
         None, None, None),
        (28, "pdf", "Apunte 4: Sistemas lineales", "failed", None, DOWNLOAD_FAILED_TEXT, 706, None, None, None,
         None, FAIL_DOWNLOAD, f"{pluginfile}/36/mod_resource/content/1/apunte4.pdf"),
        (29, "file", "Programa de la materia.docx", "unsupported", None, NOT_READABLE_YET, 707, None, None, None, None,
         None, None),
    ]
    for (mid, kind, title, status, summary, error, module_id, parent, page_from, page_to, pages, fail_kind,
         remote_url) in rows:
        conn.execute(
            "INSERT INTO material(id, section_id, kind, title, status, summary_md, error, updated_at,"
            " file_key, module_id, module_kind, parent_id, page_from, page_to, pages, fail_kind, remote_url)"
            " VALUES (?, 11, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, kind, title, status, summary, error, at,
             f"{module_id}:/{mid}" if module_id else None, module_id, "resource" if module_id else None,
             parent, page_from, page_to, pages, fail_kind, remote_url),
        )
    conn.execute("INSERT INTO topic VALUES (6, 101, 'polinomios', 'Polinomios y sistemas')")
    conn.executemany("INSERT INTO material_topic VALUES (?, 6)", [(21,), (24,)])
    for qid, mat, stem, options, correct, expl, err, page in DOCUMENT_QUESTIONS:
        conn.execute(
            "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
            " explanation, common_error, source_page, verified, dedupe_key)"
            " VALUES (?, ?, 6, ?, ?, ?, ?, ?, ?, 1, ?)",
            (qid, mat, stem, json.dumps(options, ensure_ascii=False), correct, expl, err, page, f"q{qid}"),
        )
    conn.commit()
    conn.close()


def seed_pretest(db_path: Path, now: int) -> None:
    """Una clase preparada hace una hora, con 3 preguntas verificadas y ninguna respuesta."""
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status, summary_md, updated_at)"
        " VALUES (5, 10, 505, 'youtube', 'Video-clase 4: Distancia y punto medio.', 'ysz5S6PUM-U', 'done', ?, ?)",
        (SUMMARY_5, now - 3600),
    )
    conn.execute("INSERT INTO topic VALUES (4, 101, 'distancia', 'Distancia y punto medio')")
    conn.execute("INSERT INTO material_topic VALUES (5, 4)")
    for qid, stem, options, correct, expl, err, sec in PRETEST_QUESTIONS:
        conn.execute(
            "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
            " explanation, common_error, source_seconds, verified, dedupe_key)"
            " VALUES (?, 5, 4, ?, ?, ?, ?, ?, ?, 1, ?)",
            (qid, stem, json.dumps(options, ensure_ascii=False), correct, expl, err, sec, f"q{qid}"),
        )
    conn.commit()
    conn.close()


def seed_agenda(db_path: Path, now: int) -> None:
    """Horarios, entregas, exámenes manuales, disponibilidad, plan y un simulacro terminado."""
    conn = connect(db_path)
    # Una materia más, sin horarios y con la hora en el nombre: aparece como sugerencia en Ajustes.
    conn.execute("INSERT INTO course VALUES (30146, ?, 'Física I - 14:00 a 16:00 hs', 'FIS')", (SITE_ID,))
    conn.commit()
    add_slot(conn, 101, 0, 510, 630, "Aula 204")          # lunes 8:30 a 10:30
    add_slot(conn, 101, 3, 1080, 1200, "Aula 112")        # jueves 18:00 a 20:00
    add_slot(conn, 101, datetime.fromtimestamp(now, TZ).weekday(), 1290, 1380, "Zoom")  # hoy, 21:30 a 23
    conn.executemany(
        "INSERT INTO deadline(course_id, kind, ext_id, name, due_at, url) VALUES (101, ?, ?, ?, ?, ?)",
        [
            ("assign", 1, "Trabajo práctico 1: rectas", local_at(now, 2, 23, 59),
             "https://aulasvirtuales.frba.utn.edu.ar/mod/assign/view.php?id=901"),
            ("event", 2, "Clase de consulta", local_at(now, 4, 18), None),
        ],
    )
    conn.commit()
    set_availability(conn, {0: 90, 1: 90, 2: 90, 3: 50, 4: 90, 5: 120, 6: 0})
    # Examen a mano en 5 días: sus sesiones son de repaso mezclado. Y uno ya rendido, sin nota.
    create_manual_exam(conn, 101, "Parcialito de rectas", local_at(now, 5, 18), [1, 2], 10)
    create_manual_exam(conn, 101, "Evaluación diagnóstica", local_at(now, -3, 18), [], 10)
    # Dos sesiones de foco hechas por tema: las próximas de esos temas ya van mezcladas.
    for days_ago, topic in ((6, 1), (5, 2), (4, 1), (3, 2)):
        conn.execute(
            "INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes, done, kind, topics_json) "
            "VALUES (101, ?, 1, ?, 50, 1, 'focus', ?)",
            (topic, local_at(now, -days_ago, 19), json.dumps([topic])),
        )
    conn.commit()
    # Un recuperatorio cargado hace 3 días (el simulacro corto se sugiere hoy) y un final a menos
    # de 4 semanas ("el plan usa el tiempo que hay").
    create_manual_exam(conn, 101, "Recuperatorio de funciones lineales", local_at(now, 9, 18), [], 10,
                       kind=RETAKE, now=now - 3 * DAY)
    create_manual_exam(conn, 101, "Final de Matemática", local_at(now, 20, 9), [], 10, kind=FINAL, now=now)
    replan(conn, now)
    # Un simulacro terminado: 2 de 6 bien (nota 4 de 12), con una predicción de 9.
    attempt = start_mock(conn, 1, now - DAY, predicted_grade=9)
    answers = {item.question_id: (0 if item.question_id % 3 else 1) for item in attempt.items}
    finish_mock(conn, attempt.attempt_id, answers, now - DAY + 600)
    # Un examen ya rendido, con simulacro antes (esperaba 8) y la nota real cargada: "Cómo vengo".
    graded = create_manual_exam(conn, 101, "Parcialito de funciones lineales", local_at(now, -10, 18), [1, 2], 10)
    attempt = start_mock(conn, graded, now - 12 * DAY, predicted_grade=8)
    answers = {item.question_id: (0 if item.question_id % 2 else 1) for item in attempt.items}
    finish_mock(conn, attempt.attempt_id, answers, now - 12 * DAY + 900)
    set_exam_result(conn, graded, 6, now - 9 * DAY)
    # Una inscripción abierta desde ayer que cierra en 2 días a las 23:59: entra en el aviso de
    # los 3 días antes del cierre.
    opens, closes = local_at(now, -1, 9), local_at(now, 2, 23, 59)
    create_manual_exam(conn, 101, "Inscripción a finales", opens, [], None, kind=ENROLLMENT, closes_at=closes, now=now)
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--step", choices=["facultad", "login", "ia", "listo", "vencida"], default="listo")
    parser.add_argument("--no-exam", action="store_true")
    args = parser.parse_args()

    keyring.set_keyring(MemoryKeyring())
    folder = Path(tempfile.mkdtemp(prefix="estudia-dev-"))
    # Parciales y apuntes van a la carpeta temporal, nunca a la de datos de verdad.
    os.environ[DATA_DIR_ENV] = str(folder)
    db_path = folder / "estudia.db"
    seed(db_path, with_exam=not args.no_exam)

    conn = connect(db_path)
    if args.step != "facultad":
        conn.execute("INSERT INTO kv VALUES (?, ?)", (SITE_KEY, SITE_ID))
    if args.step in ("ia", "listo", "vencida"):
        # Ya contestó la pregunta de edad (adulto), así que Ajustes muestra la sección "Edad".
        conn.execute("INSERT OR REPLACE INTO kv VALUES ('age_gate', 'adult')")
    if args.step in ("facultad", "login"):
        # Instalación nueva: todavía no sincronizó nunca.
        conn.execute("DELETE FROM kv WHERE key='last_sync_at'")
    conn.commit()
    conn.close()
    if args.step in ("ia", "listo"):
        set_secret(moodle_token_name(SITE_ID), "token-de-prueba")
    if args.step == "listo":
        # El enlace del calendario del campus, ya buscado y guardado ("Mostrar enlace" en la Agenda).
        set_secret(feed_secret_name(SITE_ID), export_url(SITE_URL, 1, "token-del-calendario-de-prueba"))
        conn = connect(db_path)
        conn.execute("INSERT OR REPLACE INTO kv VALUES (?, 'saved')", (feed_status_key(SITE_ID),))
        conn.commit()
        conn.close()
    if args.step in ("listo", "vencida"):
        set_secret(GEMINI_KEY, "clave-de-prueba")

    app = create_app(
        db_path=db_path,
        moodle_client_factory=OfflineMoodle,
        ai_provider_factory=lambda key: QuotaProvider(),
        open_browser=lambda url: print(f"(se abriría el navegador en {url})"),
        port=args.port,
    )
    print(f"Estudia de prueba en http://{HOST}:{args.port}/  (base: {db_path})")
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
