import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import {
  ApiError,
  api,
  type AgendaItem,
  type AgendaKind,
  type AgendaWeek,
  type Course,
  type ExamInfo,
  type ExamKind,
  type QuizParams,
  type SessionInfo,
} from "../api";
import { clock, dayKey, localToEpoch, tidy, weekdayAndDay } from "../format";
import { Button, FIELD, Notice } from "../ui";
import { HealthLine } from "./Health";
import Phone from "./Phone";

const WEEK_S = 7 * 86400;

/** El tipo se dice con texto chico, no con colores. Clase y estudio se entienden solos. */
const KIND_LABEL: Partial<Record<AgendaKind, string>> = {
  deadline: "Entrega",
  exam: "Examen",
  event: "Evento",
};

interface Props {
  onPractice: (params: QuizParams, session?: SessionInfo) => void;
  /** El botón del simulacro corto ("Hoy: simulacro corto") de un recuperatorio. */
  onShortMock?: (examId: number) => void;
}

/** Con qué arranca el alta: el tipo y, desde "Armar el recuperatorio", la materia y el parcial de origen. */
interface AddInitial {
  kind: ExamKind;
  courseId?: number;
  /** El parcial del que sale el recuperatorio: sus temas son los del recuperatorio. */
  fromExamId?: number;
}

export default function Agenda({ onPractice, onShortMock }: Props) {
  const [offset, setOffset] = useState(0);
  const [week, setWeek] = useState<AgendaWeek | null>(null);
  const [failed, setFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [adding, setAdding] = useState<AddInitial | null>(null);
  const [addKey, setAddKey] = useState(0);
  const [exams, setExams] = useState<ExamInfo[]>([]);

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

  // Solo para "Armar el recuperatorio": si falla, la agenda se ve igual, sin esa opción.
  useEffect(() => {
    let alive = true;
    api
      .exams()
      .then((list) => alive && setExams(list))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [reload]);

  const now = Date.now() / 1000;
  // Exámenes ya rendidos y con nota: desde ahí se arma el recuperatorio (examen → materia).
  const retakes = new Map(
    exams.filter((e) => e.kind === "parcial" && e.at != null && e.at < now && e.result).map((e) => [e.id, e.course_id]),
  );

  function openAdd(initial: AddInitial) {
    setAdding(initial);
    setAddKey((k) => k + 1);
  }

  const today = dayKey(now);
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
          <Button
            variant="secondary"
            onClick={() => (adding ? setAdding(null) : openAdd({ kind: "parcial" }))}
            aria-expanded={adding !== null}
          >
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
          key={addKey}
          initial={adding}
          exams={exams}
          onCancel={() => setAdding(null)}
          onSaved={() => {
            setAdding(null);
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
                <HealthLine key={h.exam_id} health={h} onPractice={onPractice} onShortMock={onShortMock} />
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
                retakes={retakes}
                onRetake={(examId, courseId) => openAdd({ kind: "recuperatorio", courseId, fromExamId: examId })}
              />
            ))}
          </div>
          <Phone />
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
  retakes,
  onRetake,
}: {
  date: string;
  items: AgendaItem[];
  isToday: boolean;
  showCourse: boolean;
  onPractice: Props["onPractice"];
  retakes: Map<number, number>;
  onRetake: (examId: number, courseId: number) => void;
}) {
  const name = weekdayAndDay(date);
  return (
    <section aria-label={name} aria-current={isToday ? "date" : undefined} className="min-w-0">
      <h2 className={`mb-2 border-b border-line pb-2 text-sm first-letter:uppercase ${isToday ? "font-semibold" : "text-muted"}`}>
        {name}
      </h2>
      {items.length ? (
        <ul className="space-y-4 lg:space-y-5">
          {items.map((item, i) => (
            <Item
              key={`${item.kind}-${item.id}-${item.phase ?? ""}-${item.starts_at}`}
              item={item}
              continues={item.kind === "session" && previousSession(items, i)?.title === item.title}
              showCourse={showCourse}
              onPractice={onPractice}
              onRetake={item.kind === "exam" && retakes.has(item.id) ? () => onRetake(item.id, retakes.get(item.id)!) : undefined}
            />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-faint">Libre</p>
      )}
    </section>
  );
}

const SESSION_KINDS = ["Foco", "Repaso mezclado"];

/** "Repaso mezclado: Límites y Derivadas" → tipo "Repaso mezclado" y temas "Límites y Derivadas". */
function splitSessionTitle(title: string): { kind: string | undefined; topics: string } {
  const kind = SESSION_KINDS.find((k) => title.startsWith(`${k}: `));
  return kind ? { kind, topics: title.slice(kind.length + 2) } : { kind: undefined, topics: title };
}

/** La última sesión del día antes de la posición `index`. */
function previousSession(items: AgendaItem[], index: number): AgendaItem | undefined {
  for (let i = index - 1; i >= 0; i--) if (items[i].kind === "session") return items[i];
  return undefined;
}

function Item({
  item,
  continues = false,
  showCourse,
  onPractice,
  onRetake,
}: {
  item: AgendaItem;
  /** La sesión anterior del día es igual: se dice que sigue, sin repetir los temas. */
  continues?: boolean;
  showCourse: boolean;
  onPractice: Props["onPractice"];
  /** Solo en un examen ya rendido con nota. */
  onRetake?: () => void;
}) {
  const done = item.kind === "session" && item.done;
  const course = showCourse && item.course && tidy(item.course) !== tidy(item.title) ? tidy(item.course) : null;
  // Una inscripción aparece dos veces, el día que abre y el día que cierra: el rótulo chico lo dice.
  // En una sesión, el rótulo chico es el tipo ("Foco", "Repaso mezclado") y el título, los temas.
  const session = item.kind === "session" ? splitSessionTitle(item.title) : null;
  const label =
    item.kind === "enrollment" ? (item.phase === "close" ? "Cierra" : "Abre") : (session?.kind ?? KIND_LABEL[item.kind]);
  const title = continues ? "Sigue el mismo repaso" : session ? session.topics : tidy(item.title);
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
            className={`${item.kind === "exam" ? "font-semibold" : ""} ${continues ? "text-muted" : ""} ${done ? "text-muted line-through decoration-line" : ""}`}
          >
            {title}
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
        {onRetake && (
          <Button variant="quiet" className="text-sm lg:text-xs" onClick={onRetake}>
            Armar el recuperatorio
          </Button>
        )}
      </div>
    </li>
  );
}

const KIND_OPTIONS: { kind: ExamKind; label: string; placeholder: string }[] = [
  { kind: "parcial", label: "Parcial", placeholder: "Primer parcial" },
  { kind: "recuperatorio", label: "Recuperatorio", placeholder: "Recuperatorio del primer parcial" },
  { kind: "final", label: "Final", placeholder: "Final" },
  { kind: "inscripcion", label: "Inscripción", placeholder: "Inscripción a finales" },
];

/**
 * Alta de un examen a mano: tipo, materia, nombre, día, hora y, si se sabe, la nota máxima.
 * Una inscripción pide cuándo abre y cuándo cierra, y no lleva nota.
 */
function AddExam({
  initial,
  exams,
  onCancel,
  onSaved,
}: {
  initial: AddInitial;
  exams: ExamInfo[];
  onCancel: () => void;
  onSaved: () => void;
}) {
  const id = useId();
  const [courses, setCourses] = useState<Course[] | null>(null);
  const [courseId, setCourseId] = useState("");
  const [kind, setKind] = useState<ExamKind>(initial.kind);
  const nameRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  const [date, setDate] = useState("");
  const [time, setTime] = useState("09:00");
  const [maxGrade, setMaxGrade] = useState("");
  const [closeDate, setCloseDate] = useState("");
  const [closeTime, setCloseTime] = useState("23:59");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .courses()
      .then((list) => {
        if (!alive) return;
        setCourses(list);
        const chosen = list.find((c) => c.id === initial.courseId) ?? list[0];
        if (chosen) setCourseId(String(chosen.id));
      })
      .catch(() => alive && setError("No se pudieron cargar las materias."));
    return () => {
      alive = false;
    };
  }, []);

  // Desde "Armar el recuperatorio" el tipo y la materia ya están: el foco va al nombre (y el
  // formulario, que está arriba de la semana, queda a la vista).
  const fromRetake = initial.courseId != null;
  useEffect(() => {
    if (fromRetake) nameRef.current?.focus();
  }, [fromRetake]);

  const enrollment = kind === "inscripcion";
  const placeholder = KIND_OPTIONS.find((o) => o.kind === kind)?.placeholder ?? "";

  async function save() {
    const at = localToEpoch(date, time);
    const closesAt = enrollment ? localToEpoch(closeDate, closeTime) : null;
    const scale = !enrollment && maxGrade.trim() ? Number(maxGrade.replace(",", ".")) : null;
    if (!courseId) return setError("Elegí la materia.");
    if (!name.trim()) return setError(`Poné un nombre, por ejemplo "${placeholder}".`);
    if (at === null) return setError(enrollment ? "Elegí cuándo abre." : "Elegí el día y la hora.");
    if (enrollment && closesAt === null) return setError("Elegí cuándo cierra.");
    if (closesAt !== null && closesAt <= at) return setError("El cierre tiene que ser después de la apertura.");
    if (scale !== null && !(scale > 0)) return setError("La nota máxima tiene que ser un número mayor que cero.");
    // Un recuperatorio armado desde un parcial abarca los temas de ese parcial, no toda la materia.
    const source = exams.find((e) => e.id === initial.fromExamId);
    const topicIds =
      kind === "recuperatorio" && source && String(source.course_id) === courseId ? source.topic_ids : null;
    setBusy(true);
    setError(null);
    try {
      await api.createExam({
        course_id: Number(courseId),
        name: name.trim(),
        at,
        ...(closesAt !== null ? { closes_at: closesAt } : {}),
        max_grade: scale,
        kind,
        ...(topicIds !== null ? { topic_ids: topicIds } : {}),
      });
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
          <label htmlFor={`${id}-kind`} className={label}>
            Tipo
          </label>
          <select
            id={`${id}-kind`}
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as ExamKind);
              setError(null);
            }}
            className={`w-full cursor-pointer ${FIELD}`}
          >
            {KIND_OPTIONS.map((o) => (
              <option key={o.kind} value={o.kind}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
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
        {kind === "recuperatorio" && (
          <p className="text-sm text-muted sm:col-span-2">
            Entran solo los temas que todavía no tenés firmes, sin contenido nuevo.
          </p>
        )}
        {kind === "final" && (
          <p className="text-sm text-muted sm:col-span-2">El plan arranca 6 semanas antes, con todos los temas de la materia.</p>
        )}
        <div>
          <label htmlFor={`${id}-name`} className={label}>
            Nombre
          </label>
          <input
            id={`${id}-name`}
            type="text"
            value={name}
            ref={nameRef}
            placeholder={placeholder}
            onChange={(e) => setName(e.target.value)}
            className={`w-full ${FIELD}`}
          />
        </div>
        <DateTime
          id={`${id}-at`}
          dateLabel={enrollment ? "Abre" : "Día"}
          timeName={enrollment ? "Hora de apertura" : undefined}
          date={date}
          time={time}
          onDate={setDate}
          onTime={setTime}
          className="sm:col-start-1"
        />
        {enrollment ? (
          <DateTime
            id={`${id}-close`}
            dateLabel="Cierra"
            timeName="Hora de cierre"
            date={closeDate}
            time={closeTime}
            onDate={setCloseDate}
            onTime={setCloseTime}
          />
        ) : (
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
        )}
      </div>
      {error && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error}
        </p>
      )}
      <div className="mt-6 flex flex-wrap gap-3">
        <Button type="submit" disabled={busy}>
          {enrollment ? "Guardar inscripción" : "Guardar examen"}
        </Button>
        <Button variant="quiet" onClick={onCancel}>
          Cancelar
        </Button>
      </div>
    </form>
  );
}

/** Día y hora en una fila. `timeName`: cómo se llama la hora para el lector de pantalla si hay dos en el formulario. */
function DateTime({
  id,
  dateLabel,
  timeName,
  date,
  time,
  onDate,
  onTime,
  className = "",
}: {
  id: string;
  dateLabel: string;
  timeName?: string;
  date: string;
  time: string;
  onDate: (value: string) => void;
  onTime: (value: string) => void;
  className?: string;
}) {
  const label = "mb-1.5 block text-sm font-medium";
  return (
    <div className={`grid grid-cols-[minmax(0,1fr)_7rem] gap-3 ${className}`}>
      <div>
        <label htmlFor={`${id}-date`} className={label}>
          {dateLabel}
        </label>
        <input
          id={`${id}-date`}
          type="date"
          value={date}
          onChange={(e) => onDate(e.target.value)}
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
          aria-label={timeName}
          onChange={(e) => onTime(e.target.value)}
          className={`w-full ${FIELD}`}
        />
      </div>
    </div>
  );
}
