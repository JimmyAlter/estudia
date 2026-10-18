"""Vuelve a verificar a ciegas las preguntas ya guardadas y retira las que no pasan.

Las preguntas guardadas con el lector viejo de la pasada a ciegas (una lista de
índices sin número de pregunta) pudieron pasar con respuestas desordenadas. Esto
repite la pasada con el lector estricto, un pedido por material y con otro modelo
que el que hizo el análisis. Cada material terminado queda anotado en kv
("reverified:<id>"): si se corta por cuota, la próxima corrida sigue donde quedó.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from types import SimpleNamespace

from estudia.ai.base import AIAuthError, AIFormatError, AIProvider, AIQuotaError, AIUnavailableError
from estudia import config
from estudia.ai.quota import record_usage
from estudia.generation.pipeline import blind_matches, load_exhausted, save_exhausted

REVERIFIED_PREFIX = "reverified:"


@dataclass
class ReverifyResult:
    materials_checked: int = 0
    kept: int = 0
    retired: int = 0
    materials_left: int = 0  # sin verificar: cuota, Gemini caído o respuestas ilegibles
    stop_reason: str | None = None  # quota | unavailable | auth


def _pending_materials(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id, m.analysis_model FROM material m"
        " WHERE EXISTS (SELECT 1 FROM question q WHERE q.material_id = m.id AND q.retired = 0)"
        " AND NOT EXISTS (SELECT 1 FROM kv WHERE kv.key = ? || m.id)"
        " ORDER BY m.id",
        (REVERIFIED_PREFIX,),
    ).fetchall()


def reverify_all(conn: sqlite3.Connection, provider: AIProvider) -> ReverifyResult:
    """Repite la pasada a ciegas de cada material con preguntas activas; retira las que fallan.

    Sin modelo de análisis anotado no se sabe cuál fue: se excluye toda la cadena de
    análisis, así la verificación nunca la hace el mismo modelo. Los pedidos hechos se suman
    al uso del día de la cuota, también si la corrida se corta.
    """
    result = ReverifyResult()
    load_exhausted(conn, provider)
    materials = _pending_materials(conn)
    try:
        for done, material in enumerate(materials):
            rows = conn.execute(
                "SELECT id, stem, options_json, correct_index FROM question"
                " WHERE material_id=? AND retired=0 ORDER BY id",
                (material["id"],),
            ).fetchall()
            questions = [
                SimpleNamespace(id=r["id"], stem=r["stem"], options=json.loads(r["options_json"]),
                                correct_index=r["correct_index"])
                for r in rows
            ]
            # Sin modelo registrado se excluye toda la cadena de análisis vigente (lo conservador).
            exclude = ((material["analysis_model"],) if material["analysis_model"]
                       else tuple(config.MODEL_CHAINS["analysis"]))
            try:
                matches = blind_matches(provider, questions, exclude)
            except AIFormatError:
                result.materials_left += 1  # ningún modelo respondió legible: queda para otra vez
                continue
            except (AIQuotaError, AIUnavailableError, AIAuthError) as exc:
                result.stop_reason = (
                    "quota" if isinstance(exc, AIQuotaError)
                    else "unavailable" if isinstance(exc, AIUnavailableError) else "auth"
                )
                result.materials_left += len(materials) - done
                break
            failed = [q.id for q, ok in zip(questions, matches) if not ok]
            conn.executemany("UPDATE question SET retired=1 WHERE id=?", [(i,) for i in failed])
            conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, '1')",
                         (f"{REVERIFIED_PREFIX}{material['id']}",))
            conn.commit()
            result.materials_checked += 1
            result.retired += len(failed)
            result.kept += len(questions) - len(failed)
    finally:
        save_exhausted(conn, provider)
        record_usage(conn, provider, time.time())
    return result
