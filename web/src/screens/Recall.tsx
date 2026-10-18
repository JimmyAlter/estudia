import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { api, type RecallHeading } from "../api";
import { Button, FIELD, Label } from "../ui";

/** El reloj, en segundos: una guía, no un límite. */
export const RECALL_SECONDS = 180;

/** Solo minutos, redondeados para arriba: "3 min", "2 min", "1 min". Sin cuenta por segundo. */
function minutesLeft(seconds: number): string {
  return `${Math.ceil(seconds / 60)} min`;
}

/**
 * "Contá lo que te acordás": antes del resumen, escribir de memoria lo de la clase. Después se ve lo
 * escrito al lado del resumen, con los títulos que no aparecieron marcados.
 */
export default function Recall({
  materialId,
  summary,
  onSkip,
  onCompared,
}: {
  materialId: number;
  /** El resumen, con los títulos no mencionados marcados. */
  summary: (missed: Set<string>) => ReactNode;
  /** Saltear: se abre el resumen de siempre, sin comparar nada. */
  onSkip: () => void;
  /** Ya está la comparación a la vista. */
  onCompared?: () => void;
}) {
  const id = useId();
  const [text, setText] = useState("");
  const [left, setLeft] = useState(RECALL_SECONDS);
  const [headings, setHeadings] = useState<RecallHeading[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  const resultRef = useRef<HTMLDivElement>(null);

  // Cuenta desde que se abre; se calcula con la hora para no atrasarse si la pestaña duerme.
  useEffect(() => {
    const started = Date.now();
    const timer = window.setInterval(() => {
      const remaining = Math.max(0, RECALL_SECONDS - Math.floor((Date.now() - started) / 1000));
      setLeft(remaining);
      if (remaining === 0) window.clearInterval(timer);
    }, 1000);
    fieldRef.current?.focus();
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (headings) resultRef.current?.focus();
  }, [headings]);

  async function finish() {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(false);
    try {
      const result = await api.recall(materialId, text);
      setHeadings(result.headings);
      onCompared?.();
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  if (headings) {
    const missed = new Set(headings.filter((h) => !h.mentioned).map((h) => h.title));
    return (
      <div ref={resultRef} tabIndex={-1} className="reveal outline-none">
        <p className="mb-6 text-sm text-muted">
          {missed.size
            ? "Lo que no mencionaste está marcado en el resumen: es lo que conviene repasar."
            : "Mencionaste todos los títulos del resumen."}
        </p>
        <div className="grid gap-x-12 gap-y-8 lg:grid-cols-2">
          <section>
            <Label className="mb-2">Lo que escribiste</Label>
            <p className="max-w-[65ch] text-[1.0625rem] leading-relaxed whitespace-pre-line [overflow-wrap:anywhere]">{text}</p>
          </section>
          <section>
            <Label className="mb-2">Resumen</Label>
            {summary(missed)}
          </section>
        </div>
      </div>
    );
  }

  const done = left === 0;
  return (
    <form
      className="max-w-xl"
      onSubmit={(e) => {
        e.preventDefault();
        finish();
      }}
    >
      <div className="mb-1.5 flex items-baseline justify-between gap-4">
        <label htmlFor={id} className="font-medium">
          Contá lo que te acordás
        </label>
        {!done && (
          // Para lectores de pantalla no se anuncia cada minuto: solo el aviso del final.
          <span className="text-sm tabular-nums text-muted" aria-hidden>
            {minutesLeft(left)}
          </span>
        )}
      </div>
      <p id={`${id}-help`} className="mb-3 text-sm text-muted">
        Escribí lo que recuerdes de la clase, con tus palabras. Después lo comparás con el resumen.
      </p>
      <textarea
        id={id}
        ref={fieldRef}
        rows={7}
        maxLength={5000}
        value={text}
        onChange={(e) => setText(e.target.value)}
        aria-describedby={`${id}-help`}
        className={`block w-full resize-y text-[1.0625rem] leading-relaxed ${FIELD}`}
      />
      {/* Un hilo que se vacía de a poco: marca el tiempo sin apurar. */}
      <span className="mt-3 block h-px overflow-hidden bg-line" aria-hidden>
        <span
          className="block h-full origin-left bg-faint transition-transform duration-1000 ease-linear motion-reduce:transition-none"
          style={{ transform: `scaleX(${left / RECALL_SECONDS})` }}
        />
      </span>
      <p role="status" className="mt-2 min-h-[1.5rem] text-sm text-muted">
        {done ? "Pasaron 3 minutos. Podés seguir o terminar." : ""}
      </p>
      <div className="mt-3 flex flex-wrap items-baseline gap-x-5 gap-y-3">
        <Button type="submit" variant="secondary" disabled={!text.trim() || busy}>
          Terminar
        </Button>
        <Button variant="quiet" className="text-sm" onClick={onSkip}>
          Saltear
        </Button>
      </div>
      {error && (
        <p role="alert" className="mt-3 text-sm text-bad">
          No se pudo comparar. Probá de nuevo.
        </p>
      )}
    </form>
  );
}
