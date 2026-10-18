import { useCallback, useEffect, useId, useState } from "react";
import { ApiError, api, type Age, type Course, type Slot, type SlotBody, type SlotSuggestion, type VersionInfo } from "../api";
import { inputToMinutes, minutesToClock, minutesToInput } from "../format";
import { Button, FIELD, Notice } from "../ui";

const DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"];
const capital = (text: string) => text.charAt(0).toLocaleUpperCase("es") + text.slice(1);

/**
 * El nombre de la materia sin el horario que trae pegado (ya se muestra aparte):
 * "Física I - 14:00 a 16:00 hs" → "Física I"; "AM1 - 8.30 HS" → "AM1".
 */
export function courseWithoutTime(name: string): string {
  const cleaned = name.replace(/[\s\-–(,]*\b\d{1,2}(?:[.:]\d{2})?\s*(?:hs\b|a\s+\d|[-–]\s*\d).*$/i, "").trim();
  return cleaned || name;
}

type Prefs = { reminders: boolean; age: Age | null; allowDownload: boolean };

export default function Settings({ onChange }: { onChange?: () => void } = {}) {
  // Una sola lectura de los ajustes para Recordatorios y Edad.
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .settings()
      .then((s) => alive && setPrefs({ reminders: s.reminders, age: s.age ?? null, allowDownload: s.allow_download ?? false }))
      .catch(() => alive && setLoadError("No se pudieron cargar los ajustes."));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="max-w-2xl">
      <h1 className="mb-12 font-serif text-[2rem] leading-tight tracking-[-0.02em]">Ajustes</h1>
      <Schedule />
      <StudyTime />
      <Reminders prefs={prefs} setPrefs={setPrefs} loadError={loadError} />
      <YoutubeVideos prefs={prefs} setPrefs={setPrefs} loadError={loadError} />
      <AgeSetting prefs={prefs} setPrefs={setPrefs} loadError={loadError} onChange={onChange} />
      <Version />
    </div>
  );
}

function SectionTitle({ children, note }: { children: string; note?: string }) {
  return (
    <div className="mb-5">
      <h2 className="font-semibold">{children}</h2>
      {note && <p className="mt-1 text-sm text-muted">{note}</p>}
    </div>
  );
}

// --- Horarios de cursada -----------------------------------------------------------------------

/** Lo que se está editando: un horario existente, uno nuevo o uno que viene de una sugerencia. */
type Editing = { slot: SlotBody; id: number | null };

function Schedule() {
  const [slots, setSlots] = useState<Slot[] | null>(null);
  const [suggestions, setSuggestions] = useState<SlotSuggestion[]>([]);
  const [courses, setCourses] = useState<Course[]>([]);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.schedule();
      setSlots(data.slots);
      setSuggestions(data.suggestions);
    } catch {
      setError("No se pudieron cargar los horarios. Probá recargar la página.");
    }
  }, []);

  useEffect(() => {
    load();
    api.courses().then(setCourses).catch(() => {});
  }, [load]);

  async function remove(slot: Slot) {
    setError(null);
    try {
      await api.deleteSlot(slot.id);
      await load();
    } catch {
      setError("No se pudo borrar el horario. Probá de nuevo.");
    }
  }

  async function dismiss(s: SlotSuggestion) {
    setError(null);
    try {
      await api.dismissSuggestion(s.course_id);
      setSuggestions((list) => list.filter((x) => x.course_id !== s.course_id));
    } catch {
      setError("No se pudo descartar. Probá de nuevo.");
    }
  }

  const newSlot = (): SlotBody => ({ course_id: courses[0]?.id ?? 0, weekday: 0, start_min: 1080, end_min: 1200, place: null });

  return (
    <section className="mb-16">
      <SectionTitle note="Con tus horarios, el plan de estudio no se superpone con la cursada.">
        Horarios de cursada
      </SectionTitle>
      {slots && slots.length === 0 && <Notice>Todavía no cargaste horarios.</Notice>}
      {slots && slots.length > 0 && (
        <ul className="border-t border-line">
          {slots.map((slot) => (
            <li key={slot.id} className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-b border-line py-3">
              <span>
                <span className="tabular-nums">
                  {capital(DAYS[slot.weekday])}, {minutesToClock(slot.start_min)} a {minutesToClock(slot.end_min)}
                </span>
                <span className="text-muted"> · {[slot.course, slot.place].filter(Boolean).join(" · ")}</span>
              </span>
              <span className="flex gap-4 text-sm">
                <Button
                  variant="quiet"
                  onClick={() =>
                    setEditing({
                      id: slot.id,
                      slot: { course_id: slot.course_id, weekday: slot.weekday, start_min: slot.start_min, end_min: slot.end_min, place: slot.place },
                    })
                  }
                >
                  Editar
                </Button>
                <Button variant="quiet" onClick={() => remove(slot)}>
                  Borrar
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {suggestions.length > 0 && (
        <div className="mt-8">
          <ul>
            {suggestions.map((s) => (
              <li key={s.course_id} className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 py-2">
                <span className="tabular-nums">
                  {`${courseWithoutTime(s.course_name)}, ${minutesToClock(s.start_min)} a ${minutesToClock(s.end_min)}, sin día`}
                </span>
                <span className="flex gap-4 text-sm">
                  <Button
                    variant="quiet"
                    className="font-medium text-ink"
                    onClick={() =>
                      setEditing({
                        id: null,
                        slot: { course_id: s.course_id, weekday: 0, start_min: s.start_min, end_min: s.end_min, place: null },
                      })
                    }
                  >
                    Agregar
                  </Button>
                  <Button variant="quiet" onClick={() => dismiss(s)}>
                    Descartar
                  </Button>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {error && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error}
        </p>
      )}

      {editing ? (
        <SlotForm
          key={`${editing.id}-${editing.slot.course_id}`}
          initial={editing.slot}
          id={editing.id}
          courses={courses}
          onCancel={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await load();
          }}
        />
      ) : (
        <Button variant="secondary" className="mt-6" onClick={() => setEditing({ id: null, slot: newSlot() })} disabled={!courses.length}>
          Agregar horario
        </Button>
      )}
    </section>
  );
}

function SlotForm({
  initial,
  id: slotId,
  courses,
  onCancel,
  onSaved,
}: {
  initial: SlotBody;
  id: number | null;
  courses: Course[];
  onCancel: () => void;
  onSaved: () => void;
}) {
  const id = useId();
  const [courseId, setCourseId] = useState(String(initial.course_id));
  const [weekday, setWeekday] = useState(String(initial.weekday));
  const [start, setStart] = useState(minutesToInput(initial.start_min));
  const [end, setEnd] = useState(minutesToInput(initial.end_min));
  const [place, setPlace] = useState(initial.place ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    const startMin = inputToMinutes(start);
    const endMin = end === "00:00" ? 1440 : inputToMinutes(end);
    if (startMin === null || endMin === null || endMin <= startMin) {
      setError("La hora de fin tiene que ser después de la de inicio.");
      return;
    }
    const body: SlotBody = {
      course_id: Number(courseId),
      weekday: Number(weekday),
      start_min: startMin,
      end_min: endMin,
      place: place.trim() || null,
    };
    setBusy(true);
    setError(null);
    try {
      if (slotId === null) await api.addSlot(body);
      else await api.editSlot(slotId, body);
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "No se pudo guardar. Probá de nuevo.");
      setBusy(false);
    }
  }

  const label = "mb-1.5 block text-sm font-medium";
  return (
    <form
      aria-label="Horario"
      className="reveal mt-8 border-t border-line pt-6"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="sm:col-span-2">
          <label htmlFor={`${id}-course`} className={label}>
            Materia
          </label>
          <select id={`${id}-course`} value={courseId} onChange={(e) => setCourseId(e.target.value)} className={`w-full cursor-pointer ${FIELD}`}>
            {courses.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor={`${id}-day`} className={label}>
            Día
          </label>
          <select id={`${id}-day`} value={weekday} onChange={(e) => setWeekday(e.target.value)} className={`w-full cursor-pointer ${FIELD}`}>
            {DAYS.map((d, i) => (
              <option key={d} value={i}>
                {capital(d)}
              </option>
            ))}
          </select>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label htmlFor={`${id}-start`} className={label}>
              Desde
            </label>
            <input id={`${id}-start`} type="time" value={start} onChange={(e) => setStart(e.target.value)} className={`w-full ${FIELD}`} />
          </div>
          <div>
            <label htmlFor={`${id}-end`} className={label}>
              Hasta
            </label>
            <input id={`${id}-end`} type="time" value={end} onChange={(e) => setEnd(e.target.value)} className={`w-full ${FIELD}`} />
          </div>
        </div>
        <div className="sm:col-span-2">
          <label htmlFor={`${id}-place`} className={label}>
            Lugar o link <span className="font-normal text-muted">(opcional)</span>
          </label>
          <input id={`${id}-place`} type="text" value={place} onChange={(e) => setPlace(e.target.value)} className={`w-full ${FIELD}`} />
        </div>
      </div>
      {error && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error}
        </p>
      )}
      <div className="mt-6 flex flex-wrap gap-3">
        <Button type="submit" disabled={busy}>
          Guardar horario
        </Button>
        <Button variant="quiet" onClick={onCancel}>
          Cancelar
        </Button>
      </div>
    </form>
  );
}

// --- Tiempo para estudiar ----------------------------------------------------------------------

function StudyTime() {
  const id = useId();
  const [minutes, setMinutes] = useState<string[] | null>(null);
  const [start, setStart] = useState("19:00");
  const [status, setStatus] = useState<"idle" | "saving" | "saved">("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .planSettings()
      .then((s) => {
        setMinutes(s.availability.map(String));
        setStart(minutesToInput(s.study_start_min));
      })
      .catch(() => setError("No se pudo cargar tu tiempo para estudiar."));
  }, []);

  async function save() {
    if (!minutes) return;
    const availability = minutes.map((m) => (m.trim() === "" ? 0 : Number(m)));
    const startMin = inputToMinutes(start);
    if (availability.some((m) => !Number.isInteger(m) || m < 0 || m > 1440)) {
      setError("Los minutos por día van de 0 a 1440.");
      return;
    }
    if (startMin === null) {
      setError("Elegí una hora para empezar.");
      return;
    }
    setStatus("saving");
    setError(null);
    try {
      await api.savePlanSettings({ availability, study_start_min: startMin });
      setStatus("saved");
    } catch {
      setStatus("idle");
      setError("No se pudo guardar. Probá de nuevo.");
    }
  }

  const total = minutes?.reduce((sum, m) => sum + (Number(m) || 0), 0) ?? 0;

  return (
    <section className="mb-16">
      <SectionTitle note="Minutos por día que podés dedicarle. El plan arma sesiones de 25 y 50 minutos con eso.">
        Tiempo para estudiar
      </SectionTitle>
      {minutes && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <div className="grid grid-cols-4 gap-3 sm:grid-cols-7">
            {DAYS.map((day, i) => (
              <div key={day}>
                <label htmlFor={`${id}-${i}`} className="mb-1.5 block text-sm text-muted">
                  {capital(day.slice(0, 3))}
                </label>
                <input
                  id={`${id}-${i}`}
                  type="number"
                  inputMode="numeric"
                  min={0}
                  max={1440}
                  step={5}
                  aria-label={`Minutos el ${day}`}
                  value={minutes[i]}
                  onChange={(e) => {
                    const value = e.target.value;
                    setMinutes((list) => list!.map((m, j) => (j === i ? value : m)));
                    setStatus("idle");
                  }}
                  className={`w-full tabular-nums ${FIELD} px-2`}
                />
              </div>
            ))}
          </div>
          <p className="mt-2 text-sm text-muted tabular-nums">
            {(Math.round((total / 60) * 10) / 10).toLocaleString("es-AR")} h por semana
          </p>
          <div className="mt-6">
            <label htmlFor={`${id}-start`} className="mb-1.5 block text-sm font-medium">
              Hora preferida para empezar
            </label>
            <input
              id={`${id}-start`}
              type="time"
              value={start}
              onChange={(e) => {
                setStart(e.target.value);
                setStatus("idle");
              }}
              className={`w-32 ${FIELD}`}
            />
          </div>
          <div className="mt-6 flex items-baseline gap-4">
            <Button type="submit" variant="secondary" disabled={status === "saving"}>
              Guardar tiempo
            </Button>
            {status === "saved" && (
              <span role="status" className="text-sm text-muted">
                Guardado
              </span>
            )}
          </div>
        </form>
      )}
      {error && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error}
        </p>
      )}
    </section>
  );
}

// --- Recordatorios -----------------------------------------------------------------------------

type PrefsProps = { prefs: Prefs | null; setPrefs: (p: Prefs) => void; loadError: string | null };

/** Interruptor de un ajuste: guarda al tocarlo. */
function Switch({ on, label, noteId, onToggle }: { on: boolean | null; label: string; noteId: string; onToggle: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on ?? false}
      aria-label={label}
      aria-describedby={noteId}
      disabled={on === null}
      onClick={onToggle}
      className={`relative mt-1 h-5 w-9 shrink-0 cursor-pointer rounded-full transition-colors duration-150 ${on ? "bg-ink" : "bg-track"}`}
    >
      <span
        aria-hidden
        className={`absolute top-0.5 left-0.5 size-4 rounded-full bg-canvas transition-transform duration-200 ease-[var(--ease-out-strong)] ${on ? "translate-x-4" : ""}`}
      />
    </button>
  );
}

function Reminders({ prefs, setPrefs, loadError }: PrefsProps) {
  const on = prefs ? prefs.reminders : null;
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    if (!prefs) return;
    const next = !prefs.reminders;
    setPrefs({ ...prefs, reminders: next });
    setError(null);
    try {
      await api.saveSettings({ reminders: next });
    } catch {
      setPrefs(prefs);
      setError("No se pudo guardar. Probá de nuevo.");
    }
  }

  return (
    <section>
      <SectionTitle>Recordatorios</SectionTitle>
      <div className="flex items-start gap-4">
        <Switch on={on} label="Recordatorios" noteId="reminders-note" onToggle={toggle} />
        <div>
          <p>Un aviso la tarde antes de un examen, 15 minutos antes de cada clase y al empezar cada sesión de estudio</p>
          <p id="reminders-note" className="text-sm text-muted">
            Funcionan con Estudia abierta.
          </p>
        </div>
      </div>
      {(error || loadError) && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error ?? "No se pudieron cargar los recordatorios."}
        </p>
      )}
    </section>
  );
}

// --- Videos de YouTube ------------------------------------------------------------------------

function YoutubeVideos({ prefs, setPrefs, loadError }: PrefsProps) {
  const noteId = useId();
  const on = prefs ? prefs.allowDownload : null;
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    if (!prefs) return;
    const next = !prefs.allowDownload;
    setPrefs({ ...prefs, allowDownload: next });
    setError(null);
    try {
      await api.saveSettings({ allow_download: next });
    } catch {
      setPrefs(prefs);
      setError("No se pudo guardar. Probá de nuevo.");
    }
  }

  return (
    <section className="mt-16">
      <SectionTitle>Videos de YouTube</SectionTitle>
      <div className="flex items-start gap-4">
        <Switch on={on} label="Descargar los videos cuando el link no alcanza" noteId={noteId} onToggle={toggle} />
        <div>
          <p>Descargar los videos cuando el link no alcanza</p>
          <p id={noteId} className="text-sm text-muted">
            Bajarlos usa más internet y lugar en el disco. Viene apagado.
          </p>
        </div>
      </div>
      {(error || loadError) && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error ?? "No se pudo cargar este ajuste."}
        </p>
      )}
    </section>
  );
}

// --- Edad --------------------------------------------------------------------------------------

function AgeSetting({ prefs, setPrefs, loadError, onChange }: PrefsProps & { onChange?: () => void }) {
  const age = prefs?.age;
  const [error, setError] = useState<string | null>(null);

  async function choose(next: Age) {
    if (!prefs || next === prefs.age) return;
    const before = prefs;
    setPrefs({ ...prefs, age: next });
    setError(null);
    try {
      await api.saveSettings({ age: next });
      onChange?.();
    } catch {
      setPrefs(before);
      setError("No se pudo guardar. Probá de nuevo.");
    }
  }

  const options: { value: Age; label: string }[] = [
    { value: "adult", label: "Tengo 18 años o más" },
    { value: "minor", label: "Tengo menos de 18" },
  ];

  return (
    <section className="mt-16">
      <SectionTitle note="Gemini, la IA de Estudia, es para mayores de 18. Con menos, Estudia no la usa: la agenda, el plan y los recordatorios siguen andando.">
        Edad
      </SectionTitle>
      {/* Radios nativos: las flechas cambian la opción, como en cualquier grupo. */}
      <div role="radiogroup" aria-label="Edad" className="flex flex-wrap gap-x-8 gap-y-2">
        {options.map((o) => (
          <label key={o.value} className="flex cursor-pointer items-center gap-2.5">
            <input
              type="radio"
              name="age"
              value={o.value}
              checked={age === o.value}
              disabled={!prefs}
              onChange={() => choose(o.value)}
              className="size-4 cursor-pointer accent-ink"
            />
            <span className={age === o.value ? "font-medium" : "text-muted"}>{o.label}</span>
          </label>
        ))}
      </div>
      {age === "minor" && <p className="mt-3 text-sm text-muted">La clave guardada queda sin usar.</p>}
      {(error || loadError) && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error ?? "No se pudo cargar tu edad."}
        </p>
      )}
    </section>
  );
}

/** La versión instalada, al pie de Ajustes (el aviso de versión nueva se ve en toda la app). */
function Version() {
  const [info, setInfo] = useState<VersionInfo | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .version()
      .then((v) => alive && setInfo(v))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const current = info?.current ?? import.meta.env.VITE_APP_VERSION;
  if (!current) return null;
  return <p className="mt-20 border-t border-line pt-4 text-xs text-muted">Estudia {current}</p>;
}
