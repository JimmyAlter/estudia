import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ApiError, api, type PastExam } from "../api";
import { buttonClass } from "../ui";

/** Tope de parciales por examen (material/files.py, MAX_PAST_EXAMS_PER_EXAM). */
const MAX_PAST_EXAMS = 5;
const POLL_MS = 5000;

/**
 * "Cargá tus parciales viejos": subir un PDF o una foto desde la compu, ver cómo va cada uno y
 * quitarlo. Mientras alguno se está leyendo, se vuelve a consultar cada 5 segundos.
 * `onChange`: algo cambió (se cargó, se quitó o se terminó de leer uno), así el formato se actualiza.
 */
export default function PastExams({ examId, ai, onChange }: { examId: number; ai: boolean; onChange?: () => void }) {
  const id = useId();
  const [list, setList] = useState<PastExam[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [uploading, setUploading] = useState(false);
  /** El problema de la última acción, que dice qué hacer (sin rojo); `busy` (503) va en gris. */
  const [error, setError] = useState<{ text: string; busy: boolean } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const pendingIds = useRef<Set<number>>(new Set());

  const load = useCallback(async () => {
    try {
      const rows = await api.pastExams(examId);
      if (!alive.current) return;
      setList(rows);
      setLoadFailed(false);
      // Uno que se terminó de leer cambia el formato propuesto.
      const pending = new Set(rows.filter((r) => r.status === "pending").map((r) => r.id));
      const finished = [...pendingIds.current].some((pid) => !pending.has(pid));
      pendingIds.current = pending;
      if (finished) onChangeRef.current?.();
    } catch {
      if (alive.current) setLoadFailed(true);
    }
  }, [examId]);

  useEffect(() => {
    alive.current = true;
    setList(null);
    pendingIds.current = new Set();
    load();
    return () => {
      alive.current = false;
    };
  }, [load]);

  const anyPending = Boolean(list?.some((r) => r.status === "pending"));
  useEffect(() => {
    if (!anyPending) return;
    const timer = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(timer);
  }, [anyPending, load]);

  async function upload(file: File) {
    setUploading(true);
    setError(null);
    try {
      await api.uploadPastExam(examId, file);
      await load();
      onChangeRef.current?.();
    } catch (err) {
      if (!alive.current) return;
      // El servidor ya lo dice en castellano (tamaño, tipo, tope, ocupado): se muestra tal cual.
      if (err instanceof ApiError && err.status === 503) setError({ text: err.detail, busy: true });
      else if (err instanceof ApiError && err.status < 500) setError({ text: err.detail, busy: false });
      else setError({ text: "No se pudo cargar el archivo. Probá de nuevo.", busy: false });
    } finally {
      if (alive.current) setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function remove(row: PastExam) {
    setError(null);
    try {
      await api.deletePastExam(examId, row.id);
      await load();
      onChangeRef.current?.();
    } catch {
      if (alive.current) setError({ text: "No se pudo quitar. Probá de nuevo.", busy: false });
    }
  }

  const full = (list?.length ?? 0) >= MAX_PAST_EXAMS;
  return (
    <section aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="mb-1 font-semibold">
        Cargá tus parciales viejos
      </h2>
      <p className="mb-4 text-sm text-muted">PDF o foto, desde tu compu. Quedan solo en esta compu y nunca se comparten.</p>

      {loadFailed && <p className="mb-4 text-sm text-muted">No se pudieron cargar tus parciales. Probá recargar la página.</p>}
      {list && list.length > 0 && (
        <ul className="mb-5 border-t border-line">
          {list.map((row) => (
            <li key={row.id} className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-b border-line py-2.5">
              <span className="min-w-0 [overflow-wrap:anywhere]">{row.name}</span>
              <span className="flex items-baseline gap-x-5 text-sm">
                <span className="text-muted">{statusText(row, ai)}</span>
                <button
                  type="button"
                  onClick={() => remove(row)}
                  aria-label={`Quitar ${row.name}`}
                  className="press cursor-pointer text-muted underline decoration-line underline-offset-4 hover:text-ink"
                >
                  Quitar
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {list &&
        (full ? (
          <p className="text-sm text-muted">Ya cargaste 5 parciales para este examen.</p>
        ) : (
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
            {/* El selector del sistema, escondido a la vista pero no al teclado: la etiqueta es el botón. */}
            <label
              className={`${buttonClass("secondary")} inline-block has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-ink ${
                uploading ? "pointer-events-none text-muted" : ""
              }`}
            >
              Elegir archivo
              <input
                ref={inputRef}
                type="file"
                accept="application/pdf,image/jpeg,image/png"
                className="sr-only"
                disabled={uploading}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) upload(file);
                }}
              />
            </label>
            {uploading && (
              <span role="status" className="text-sm text-muted">
                Revisando el archivo…
              </span>
            )}
          </div>
        ))}
      {error && (
        <p role={error.busy ? "status" : "alert"} className={`mt-3 text-sm ${error.busy ? "text-muted" : "text-ink"}`}>
          {error.text}
        </p>
      )}
    </section>
  );
}

function statusText(row: PastExam, ai: boolean): string {
  switch (row.status) {
    case "done":
      return "Listo";
    case "failed":
      return "No se pudo leer: probá otra foto";
    default:
      // Sin IA (menor de edad o sin clave) nadie lo va a leer: el formato se carga a mano.
      return ai ? "Leyendo el formato…" : "Sin IA: cargá el formato a mano";
  }
}
