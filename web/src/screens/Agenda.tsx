import { useEffect, useId, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import {
  ApiError,
  api,
  type AgendaItem,
  type AgendaKind,
  type AgendaWeek,
  type Course,
  type QuizParams,
  type SessionInfo,
} from "../api";
import { clock, dayKey, localToEpoch, tidy, weekdayAndDay } from "../format";
import { Button, FIELD, Notice } from "../ui";
import { HealthLine } from "./Health";

const WEEK_S = 7 * 86400;

/** El tipo se dice con texto chico, no con colores. Clase y estudio se entienden solos. */
const KIND_LABEL: Partial<Record<AgendaKind, string>> = {
  deadline: "Entrega",
  exam: "Examen",
  event: "Evento",
};

interface Props {
  onPractice: (params: QuizParams, session?: SessionInfo) => void;
}

export default function Agenda({ onPractice }: Props) {
  const [offset, setOffset] = useState(0);
  const [week, setWeek] = useState<AgendaWeek | null>(null);
  const [failed, setFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    let alive = true;
    setFailed(false);
    // La semana actual va sin parámetro; las otras, con cualquier instante de esa semana.
    const start = offset === 0 ? undefined : Math.floor(Date.now() / 1000) + offset * WEEK_S;
    api
      .agenda(start)
      .then((w) => alive && setWeek(w))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [offset, reload]);

  const today = dayKey(Date.now() / 1000);
  // Con una sola materia, repetirla en cada ítem no dice nada.
  const manyCourses =
    new Set(week?.days.flatMap((d) => d.items.map((i) => (i.course ? tidy(i.course) : ""))).filter(Boolean)).size > 1;

  return (
    <div>
      <div className="mb-8 flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
        <div>
          <h1 className="font-serif text-[2rem] leading-tight tracking-[-0.02em]">Agenda</h1>
          {week && <p className="mt-1 text-muted">{weekTitle(week)}</p>}
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <Button variant="secondary" onClick={() => setAdding((a) => !a)} aria-expanded={adding}>
            Agregar examen
          </Button>
          <a
            href={api.calendarUrl}
            download
            className="text-sm text-muted underline decoration-line underline-offset-4 hover:text-ink"
          >
            Exportar calendario
          </a>
        </div>
      </div>

      <nav aria-label="Semanas" className="mb-8 flex items-center gap-1 text-sm">
        <WeekButton label="Semana anterior" onClick={() => setOffset((o) => o - 1)}>
          <ChevronLeft size={16} strokeWidth={1.75} aria-hidden />
        </WeekButton>
        <button
          type="button"
          onClick={() => setOffset(0)}
          disabled={offset === 0}
          className="press cursor-pointer rounded-md px-3 py-1.5 hover:bg-hover disabled:cursor-default disabled:text-muted disabled:hover:bg-transparent"
        >
          Hoy
        </button>
        <WeekButton label="Semana siguiente" onClick={() => setOffset((o) => o + 1)} after>
          <ChevronRight size={16} strokeWidth={1.75} aria-hidden />
        </WeekButton>
      </nav>

      {adding && (
        <AddExam
          onCancel={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            setReload((n) => n + 1);
          }}
        />
      )}

      {failed && <Notice>No se pudo cargar la agenda. Probá recargar la página.</Notice>}

      {week && (
        <>
          {week.health.length > 0 && (
            <div className="mb-10 max-w-3xl space-y-2">
              {week.health.map((h) => (
                <HealthLine key={h.exam_id} health={h} onPractice={onPractice} />
              ))}
            </div>
          )}
          <div className="grid gap-y-8 lg:grid-cols-7 lg:gap-x-4">
            {week.days.map((day) => (
              <Day
                key={day.date}
                date={day.date}
                items={day.items}
                isToday={day.date === today}
                showCourse={manyCourses}
                onPractice={onPractice}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function WeekButton({
  label,
  onClick,
  after,
  children,
}: {
  label: string;
  onClick: () => void;
  after?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="press flex cursor-pointer items-center gap-1 rounded-md px-2 py-1.5 text-muted hover:bg-hover hover:text-ink"
    >
      {!after && children}
      <span className="max-sm:hidden" aria-hidden>
        {label}
      </span>
      {after && children}
    </button>
  );
}

/** "Semana del 12 al 18 de octubre"; si cambia el mes, "del 28 de septiembre al 4 de octubre". */
function weekTitle(week: AgendaWeek): string {
  const first = week.days[0]?.date;
  const last = week.days[week.days.length - 1]?.date;
  if (!first || !last) return "";
  const fmt = (key: string, withMonth: boolean) =>
    new Intl.DateTimeFormat("es-AR", {
      day: "numeric",
      ...(withMonth ? { month: "long" } : {}),
      timeZone: "America/Argentina/Buenos_Aires",
    }).format(new Date(`${key}T12:00:00-03:00`));
  const sameMonth = first.slice(0, 7) === last.slice(0, 7);
  return `Semana del ${fmt(first, !sameMonth)} al ${fmt(last, true)}`;
}

function Day({
  date,
  items,
  isToday,
  showCourse,
  onPractice,
}: {
  date: string;
  items: AgendaItem[];
  isToday: boolean;
  showCourse: boolean;
  onPractice: Props["onPractice"];
}) {
  const name = weekdayAndDay(date);
  return (
    <section aria-label={name} aria-current={isToday ? "date" : undefined} className="min-w-0">
      <h2 className={`mb-2 border-b border-line pb-2 text-sm first-letter:uppercase ${isToday ? "font-semibold" : "text-muted"}`}>
        {name}
      </h2>
      {items.length ? (
        <ul className="space-y-4 lg:space-y-5">
          {items.map((item) => (
            <Item key={`${item.kind}-${item.id}-${item.starts_at}`} item={item} showCourse={showCourse} onPractice={onPractice} />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-faint">Libre</p>
      )}
    </section>
  );
}

function Item({
  item,
  showCourse,
  onPractice,
}: {
  item: AgendaItem;
  showCourse: boolean;
  onPractice: Props["onPractice"];
}) {
  const done = item.kind === "session" && item.done;
  const course = showCourse && item.course && tidy(item.course) !== tidy(item.title) ? tidy(item.course) : null;
  const label = KIND_LABEL[item.kind];
  return (
    <li className="grid grid-cols-[3.25rem_minmax(0,1fr)] gap-x-3 lg:block">
      <span data-testid="agenda-time" className="text-sm tabular-nums text-muted lg:block">
        {clock(item.starts_at)}
      </span>
      <div className="min-w-0">
        {label && (
          <span data-testid="agenda-kind" className="block text-[11px] font-medium uppercase tracking-[0.08em] text-muted">
            {label}
          </span>
        )}
        <p className="leading-snug lg:text-sm">
          <span
            data-testid="agenda-title"
            className={`${item.kind === "exam" ? "font-semibold" : ""} ${done ? "text-muted line-through decoration-line" : ""}`}
          >
            {tidy(item.title)}
          </span>
          {done && <span className="sr-only">Hecha</span>}
        </p>
        {course && <p className="text-sm text-muted lg:text-xs">{course}</p>}
        {item.place && <p className="text-sm text-muted lg:text-xs">{item.place}</p>}
        {item.url && (
          <a
            href={item.url}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-muted underline decoration-line underline-offset-4 hover:text-ink lg:text-xs"
          >
            Abrir en el campus
          </a>
        )}
        {item.kind === "session" && !done && (
            <Button
              variant="quiet"
              className="text-sm lg:text-xs"
              onClick={() =>
                onPractice(
                  { session_id: item.id },
                  {
                    kind: item.session_kind ?? "focus",
                    topic: item.topic ?? "",
                    review_minutes: item.review_minutes ?? 0,
                    focus_minutes: item.focus_minutes ?? 0,
                  },
                )
              }
            >
              Practicar
            </Button>
        )}
      </div>
    </li>
  );
}

/** Alta de un examen a mano: materia, nombre, día, hora y, si se sabe, la nota máxima. */
function AddExam({ onCancel, onSaved }: { onCancel: () => void; onSaved: () => void }) {
  const id = useId();
  const [courses, setCourses] = useState<Course[] | null>(null);
  const [courseId, setCourseId] = useState("");
  const [name, setName] = useState("");
  const [date, setDate] = useState("");
  const [time, setTime] = useState("09:00");
  const [maxGrade, setMaxGrade] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .courses()
      .then((list) => {
        if (!alive) return;
        setCourses(list);
        if (list[0]) setCourseId(String(list[0].id));
      })
      .catch(() => alive && setError("No se pudieron cargar las materias."));
    return () => {
      alive = false;
    };
  }, []);

  async function save() {
    const at = localToEpoch(date, time);
    const scale = maxGrade.trim() ? Number(maxGrade.replace(",", ".")) : null;
    if (!courseId) return setError("Elegí la materia.");
    if (!name.trim()) return setError("Poné un nombre, por ejemplo \"Primer parcial\".");
    if (at === null) return setError("Elegí el día y la hora.");
    if (scale !== null && !(scale > 0)) return setError("La nota máxima tiene que ser un número mayor que cero.");
    setBusy(true);
    setError(null);
    try {
      await api.createExam({ course_id: Number(courseId), name: name.trim(), at, max_grade: scale });
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "No se pudo guardar. Probá de nuevo.");
      setBusy(false);
    }
  }

  const label = "mb-1.5 block text-sm font-medium";
  return (
    <form
      aria-label="Agregar examen"
      className="reveal mb-12 max-w-2xl border-b border-line pb-8"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <h2 className="mb-4 font-semibold">Agregar examen</h2>
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor={`${id}-course`} className={label}>
            Materia
          </label>
          <select
            id={`${id}-course`}
            value={courseId}
            onChange={(e) => setCourseId(e.target.value)}
            className={`w-full cursor-pointer ${FIELD}`}
          >
            {!courses && <option value="">Cargando…</option>}
            {courses?.map((c) => (
              <option key={c.id} value={c.id}>
                {tidy(c.name)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor={`${id}-name`} className={label}>
            Nombre
          </label>
          <input
            id={`${id}-name`}
            type="text"
            value={name}
            placeholder="Primer parcial"
            onChange={(e) => setName(e.target.value)}
            className={`w-full ${FIELD}`}
          />
        </div>
        <div className="grid grid-cols-[minmax(0,1fr)_7rem] gap-3">
          <div>
            <label htmlFor={`${id}-date`} className={label}>
              Día
            </label>
            <input
              id={`${id}-date`}
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className={`w-full ${FIELD}`}
            />
          </div>
          <div>
            <label htmlFor={`${id}-time`} className={label}>
              Hora
            </label>
            <input
              id={`${id}-time`}
              type="time"
              value={time}
              onChange={(e) => setTime(e.target.value)}
              className={`w-full ${FIELD}`}
            />
          </div>
        </div>
        <div>
          <label htmlFor={`${id}-max`} className={label}>
            Nota máxima <span className="font-normal text-muted">(si no, 10)</span>
          </label>
          <input
            id={`${id}-max`}
            type="text"
            inputMode="decimal"
            value={maxGrade}
            onChange={(e) => setMaxGrade(e.target.value)}
            className={`w-28 ${FIELD}`}
          />
        </div>
      </div>
      {error && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error}
        </p>
      )}
      <div className="mt-6 flex flex-wrap gap-3">
        <Button type="submit" disabled={busy}>
          Guardar examen
        </Button>
        <Button variant="quiet" onClick={onCancel}>
          Cancelar
        </Button>
      </div>
    </form>
  );
}
