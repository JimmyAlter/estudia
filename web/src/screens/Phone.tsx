import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Copy, Download } from "lucide-react";
import { ApiError, api, type CalendarFeed } from "../api";
import { Button, FIELD } from "../ui";

const RETRY_LATER = "No se pudo consultar el campus ahora. Probá más tarde.";

/**
 * "En el celular", al pie de la Agenda: el mazo de la semana para Anki y cómo ver el
 * calendario del campus en Google Calendar.
 */
export default function Phone() {
  const id = useId();
  return (
    <section aria-labelledby={`${id}-title`} className="mt-16 max-w-2xl border-t border-line pt-8">
      <h2 id={`${id}-title`} className="mb-6 font-semibold">
        En el celular
      </h2>

      <WeeklyDeck />

      <h3 className="mt-10 mb-4 text-sm font-medium">Calendario del campus en el celular</h3>
      <ol className="space-y-4">
        <Step n={1}>
          <p>Copiá el enlace del calendario del campus.</p>
          <FeedLink />
        </Step>
        <Step n={2}>
          <p>En Google Calendar, desde la compu: Otros calendarios → + → Desde URL, y pegalo.</p>
        </Step>
        <Step n={3}>
          <p>Listo: aparece en el celular, en la app de Google Calendar. Puede tardar unas horas en actualizarse.</p>
        </Step>
      </ol>
      <p className="mt-8 text-sm text-muted">
        El calendario de Estudia se importa una vez: si cambia algo, exportalo de nuevo.
      </p>
    </section>
  );
}

/** Baja el mazo por fetch: así, sin preguntas, se dice con calma en vez de una descarga fallida. */
function WeeklyDeck() {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  async function download() {
    setBusy(true);
    setNote(null);
    try {
      const { blob, filename } = await api.weeklyAnki();
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      // Después del clic: el navegador ya tomó el archivo.
      setTimeout(() => URL.revokeObjectURL(href), 0);
    } catch (err) {
      if (!alive.current) return;
      setNote(
        err instanceof ApiError && err.status === 404
          ? "No hay preguntas para esta semana."
          : "No se pudo armar el mazo. Probá de nuevo.",
      );
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  return (
    <div>
      <button
        type="button"
        onClick={download}
        disabled={busy}
        className="press inline-flex cursor-pointer items-center gap-2 text-left font-medium underline decoration-line underline-offset-4 hover:decoration-ink disabled:cursor-default disabled:text-muted"
      >
        <Download size={16} strokeWidth={1.75} aria-hidden className="shrink-0" />
        Bajá el mazo de la semana para el celular
      </button>
      <p className="mt-1 text-sm text-muted">Se baja un archivo para abrir con Anki. Estudia no lo manda a ningún lado.</p>
      {note && (
        <p role="status" className="mt-2 text-sm text-muted">
          {note}
        </p>
      )}
    </div>
  );
}

function Step({ n, children }: { n: number; children: ReactNode }) {
  return (
    <li className="grid grid-cols-[1.5rem_minmax(0,1fr)] gap-x-2">
      <span className="text-sm tabular-nums leading-[1.6rem] text-muted" aria-hidden>
        {n}
      </span>
      <div className="min-w-0">{children}</div>
    </li>
  );
}

/**
 * El enlace del feed lleva un token del campus. Vive solo en el estado de este componente:
 * se pide al tocar "Mostrar enlace", se borra con "Ocultar" y desaparece al salir de la Agenda.
 * Nunca va a localStorage, a la dirección ni a la consola.
 */
function FeedLink() {
  const id = useId();
  const [feed, setFeed] = useState<CalendarFeed | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const field = useRef<HTMLInputElement>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    api
      .calendarFeed()
      .then((f) => alive.current && setFeed(f))
      .catch(() => alive.current && setFeed({ status: "unknown", campus_calendar_url: null }));
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(timer);
  }, [copied]);

  if (!feed) return null;

  async function find() {
    setBusy(true);
    setNote(null);
    try {
      const result = await api.fetchCalendarFeed();
      if (!alive.current) return;
      // Sin respuesta del campus: lo que se mostraba queda igual.
      if (result.retry) setNote(RETRY_LATER);
      else setFeed((f) => f && { ...f, status: result.status });
    } catch {
      if (alive.current) setNote(RETRY_LATER);
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  async function reveal() {
    setBusy(true);
    setNote(null);
    try {
      const { url } = await api.revealCalendarFeed();
      if (alive.current) setUrl(url);
    } catch (err) {
      if (!alive.current) return;
      // Ya no está guardado (se cerró la sesión del campus): hay que buscarlo de nuevo.
      if (err instanceof ApiError && err.status === 404) {
        setFeed((f) => f && { ...f, status: "unknown" });
        setNote("El enlace ya no está guardado: buscalo de nuevo.");
      }
      else setNote("No se pudo mostrar el enlace. Probá de nuevo.");
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  async function copy() {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setNote(null);
      setCopied(true);
    } catch {
      field.current?.select();
      setNote("Está seleccionado: copialo con Ctrl+C.");
    }
  }

  function hide() {
    setUrl(null);
    setCopied(false);
    setNote(null);
  }

  const noteLine = note && (
    <p role="status" className="mt-2 text-sm text-muted">
      {note}
    </p>
  );

  if (feed.status === "unavailable") {
    return (
      <div className="mt-1 text-sm text-muted">
        <p>
          En el campus: Calendario → Importar o exportar calendarios → Exportar calendario → elegí «Todos los eventos» y
          «Eventos recientes y próximos» → Obtener URL del calendario.
        </p>
        {feed.campus_calendar_url && (
          <a
            href={feed.campus_calendar_url}
            target="_blank"
            rel="noreferrer"
            className="mt-1 inline-block underline decoration-line underline-offset-4 hover:text-ink"
          >
            Abrir el calendario del campus
          </a>
        )}
      </div>
    );
  }

  if (feed.status === "unknown") {
    return (
      <div className="mt-1">
        <Button variant="quiet" className="text-sm" onClick={find} disabled={busy}>
          Buscar el enlace
        </Button>
        {noteLine}
      </div>
    );
  }

  if (!url) {
    return (
      <div className="mt-1">
        <Button variant="quiet" className="text-sm" onClick={reveal} disabled={busy}>
          Mostrar enlace
        </Button>
        {noteLine}
      </div>
    );
  }

  return (
    <div className="reveal mt-3">
      <label htmlFor={`${id}-url`} className="sr-only">
        Enlace del calendario del campus
      </label>
      <input
        id={`${id}-url`}
        ref={field}
        type="text"
        readOnly
        value={url}
        autoComplete="off"
        spellCheck={false}
        onFocus={(e) => e.currentTarget.select()}
        className={`w-full min-w-0 text-sm text-muted ${FIELD}`}
      />
      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2">
        <Button variant="secondary" className="inline-flex items-center gap-2 text-sm" onClick={copy}>
          <Copy size={14} strokeWidth={1.75} aria-hidden />
          Copiar
        </Button>
        <Button variant="quiet" className="text-sm" onClick={hide}>
          Ocultar
        </Button>
        <span role="status" className="text-sm text-muted">
          {copied ? "Copiado" : ""}
        </span>
      </div>
      {noteLine}
    </div>
  );
}
