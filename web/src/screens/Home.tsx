import { useEffect, useId, useState } from "react";
import {
  ApiError,
  api,
  type AgendaItem,
  type Age,
  type AppState,
  type ExamHealth,
  type ExamInfo,
  type HomeData,
  type PretestOffer,
  type QuizParams,
  type SessionInfo,
  type Topic,
} from "../api";
import { clock, dayKey, formatDate, formatDuration, grade, plural, shortDate, tidy } from "../format";
import { Button, FIELD, Label, Notice } from "../ui";
import { HealthLine, hasActionOrNote } from "./Health";

export type Screen = "inicio" | "agenda" | "practica" | "materiales" | "ajustes" | "simulacro" | "arrancar" | "como-vengo";

/** Con menos preguntas vistas, la probabilidad todavía dice poco: se aclara cuántas se vieron. */
const FEW_SEEN = 8;
/** Pasado este tiempo, ya no se pregunta cómo fue un examen. */
const ASK_RESULT_DAYS = 30;

interface Props {
  /** Cambia cuando hay datos nuevos (terminó una sincronización). */
  refreshKey: number;
  /** Por qué se frenó el procesamiento con Gemini, si se frenó. */
  aiStopReason: AppState["ai_stop_reason"];
  /** La respuesta de edad: sin IA (menor o sin responder) no se promete preparar clases. */
  age?: Age | null;
  /** Hay una sincronización/preparación corriendo ahora. */
  working: boolean;
  onPractice: (params: QuizParams, session?: SessionInfo) => void;
  onNavigate: (screen: Screen) => void;
  /** El botón del simulacro corto ("Hoy: simulacro corto") de un recuperatorio. */
  onShortMock?: (examId: number) => void;
  onSessionExpired: () => void;
}

export default function Home({
  refreshKey,
  aiStopReason,
  age,
  working,
  onPractice,
  onNavigate,
  onShortMock,
  onSessionExpired,
}: Props) {
  const [data, setData] = useState<HomeData | null>(null);
  const [failed, setFailed] = useState(false);
  const [health, setHealth] = useState<ExamHealth[]>([]);
  const [today, setToday] = useState<AgendaItem[]>([]);
  const [exams, setExams] = useState<ExamInfo[]>([]);

  // Lo que suma el plan: si algo falla, el inicio sigue sin esa parte.
  useEffect(() => {
    let alive = true;
    api.health().then((h) => alive && setHealth(h)).catch(() => {});
    api.exams().then((list) => alive && setExams(list)).catch(() => {});
    api
      .agenda()
      .then((week) => {
        if (!alive) return;
        const key = dayKey(Date.now() / 1000);
        setToday(week.days.find((d) => d.date === key)?.items ?? []);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [refreshKey]);

  useEffect(() => {
    let alive = true;
    api
      .home()
      .then((home) => alive && setData(home))
      .catch((err) => {
        if (!alive) return;
        if (err instanceof ApiError && err.status === 401) onSessionExpired();
        else setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [refreshKey, onSessionExpired]);

  if (failed) return <Notice>No se pudo cargar el inicio. Probá recargar la página.</Notice>;
  if (!data) return null;

  const { exam, topics, suggestion, ready } = data;
  const now = Date.now() / 1000;
  const askResult = exams
    .filter(
      (e) =>
        // Una inscripción no tiene nota: no se pregunta (el servidor la rechaza).
        e.kind !== "inscripcion" &&
        e.at != null &&
        (e.closes_at ?? e.at) <= now &&
        now - e.at < ASK_RESULT_DAYS * 86400 &&
        !e.result &&
        !e.result_skipped,
    )
    .sort((a, b) => b.at! - a.at!)[0];
  const allOnTrack = health.every((h) => h.verdict === "llegás");

  return (
    <div>
      {askResult && (
        <ResultPrompt
          key={askResult.id}
          exam={askResult}
          onDone={(change) => setExams((list) => list.map((e) => (e.id === askResult.id ? { ...e, ...change } : e)))}
        />
      )}
      <section className="mb-14">
        {exam ? (
          <>
            <Label className="mb-2">Próximo examen</Label>
            <h1 className="font-serif text-[2.6rem] leading-[1.1] tracking-[-0.025em]">{tidy(exam.name)}</h1>
            <p className="mt-3 text-muted">
              {[
                tidy(exam.course),
                `abre el ${formatDate(exam.opens_at)}`,
                // Los cargados a mano no traen intentos ni límite: no se inventan.
                exam.attempts != null && exam.time_limit_s
                  ? `${plural(exam.attempts, "intento", "intentos")}, ${formatDuration(exam.time_limit_s)}`
                  : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
            <p className="mt-1 font-medium">{daysLeft(exam.days_left)}</p>
          </>
        ) : (
          <>
            <h1 className="font-serif text-[2.6rem] leading-[1.1] tracking-[-0.025em]">Sin exámenes próximos</h1>
            <p className="mt-3 text-muted">Cuando el campus publique uno, aparece acá.</p>
          </>
        )}
        {health.length > 0 && (
          <div className="mt-6 max-w-2xl space-y-2">
            {/* Si llegás a todo, se dice una vez; abajo, solo lo que tiene algo para hacer o aclarar. */}
            {allOnTrack && <p className="text-[0.9375rem] font-medium leading-relaxed">Con tu tiempo actual llegás a todo.</p>}
            {(allOnTrack ? health.filter(hasActionOrNote) : health).map((h) => (
              <HealthLine
                key={h.exam_id}
                health={h}
                onPractice={onPractice}
                onShortMock={onShortMock}
                compact={allOnTrack}
                // El examen del título no se repite en su propia línea.
                withExamName={!(exam && h.at === exam.opens_at && h.exam === exam.name)}
              />
            ))}
          </div>
        )}
      </section>

      <div className="grid gap-x-16 gap-y-12 md:grid-cols-[minmax(0,1fr)_15rem]">
        <section>
          <h2 className="font-semibold">{exam ? "Temas que entran" : "Temas"}</h2>
          {topics.some((t) => t.total) && (
            <p className="mt-1 mb-3 text-sm text-muted">{probabilityTitle(exam?.opens_at ?? null)}</p>
          )}
          {topics.length ? (
            <ul className="mt-3">
              {topics.map((topic) => (
                <TopicRow
                  key={topic.id}
                  topic={topic}
                  headerAt={exam?.opens_at ?? null}
                  onClick={() => onPractice({ topic_id: topic.id })}
                />
              ))}
            </ul>
          ) : (
            <Notice>Todavía no hay temas. Aparecen a medida que se procesan las clases.</Notice>
          )}
        </section>

        <aside className="max-md:order-first">
          {data.pretest && (
            <PretestCard
              offer={data.pretest}
              onStart={() => onNavigate("arrancar")}
              onDismiss={() => setData((d) => d && { ...d, pretest: null })}
            />
          )}
          <Today items={today} now={now} onPractice={onPractice} />
          <h2 className="mb-3 font-semibold">Seguir estudiando</h2>
          {suggestion ? (
            <>
              <p className="mb-5 text-muted">
                {suggestion.practiced ? "Lo más flojo ahora:" : "Todavía no practicaste:"}{" "}
                <span className="text-ink">{suggestion.name}</span>.
              </p>
              <div className="flex flex-col items-start gap-3">
                <div className="flex flex-wrap gap-3">
                  <Button onClick={() => onPractice({ topic_id: suggestion.id })}>Practicar ese tema</Button>
                  {exam && (
                    <Button variant="secondary" onClick={() => onNavigate("simulacro")}>
                      Hacer un simulacro
                    </Button>
                  )}
                </div>
                {/* Con clases en cola, "Practicar" dice por qué ya se puede: reemplaza a "Práctica de todo", que hace lo mismo. */}
                {ready ? (
                  <ReadyLine ready={ready} untilTomorrow={aiStopReason === "budget"} onPractice={() => onPractice({})} />
                ) : (
                  <Button variant="quiet" className="mt-1" onClick={() => onPractice({})}>
                    Práctica de todo
                  </Button>
                )}
              </div>
            </>
          ) : ready ? (
            <ReadyLine ready={ready} untilTomorrow={aiStopReason === "budget"} onPractice={() => onPractice({})} />
          ) : (
            <Button variant="secondary" onClick={() => onNavigate("materiales")}>
              Ver materiales
            </Button>
          )}
          <Processing
            processing={data.processing}
            stopReason={aiStopReason}
            working={working}
            age={age}
            // Con preguntas listas, la línea de "Ya hay…" ya dice que lo demás sigue mañana.
            saidByReady={Boolean(ready)}
          />
          <p className="mt-10 text-sm">
            <a
              href="#/como-vengo"
              onClick={(e) => {
                e.preventDefault();
                onNavigate("como-vengo");
              }}
              className="text-muted underline decoration-line underline-offset-4 hover:text-ink"
            >
              Cómo vengo
            </a>
          </p>
        </aside>
      </div>
    </div>
  );
}

/** "3 preguntas para arrancar": una oferta tranquila, que se puede dejar pasar. */
function PretestCard({ offer, onStart, onDismiss }: { offer: PretestOffer; onStart: () => void; onDismiss: () => void }) {
  const [busy, setBusy] = useState(false);

  async function dismiss() {
    setBusy(true);
    try {
      await api.closePretest(offer.material_id);
    } catch {
      // Si no se pudo guardar, vuelve a aparecer la próxima vez: no pasa nada.
    }
    onDismiss();
  }

  return (
    <section className="mb-10 border-b border-line pb-8">
      <h2 className="mb-1 font-semibold">3 preguntas para arrancar</h2>
      <p className="mb-4 text-muted">De {tidy(offer.title)}. No cuentan: es para que la clase te rinda más.</p>
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-3">
        <Button variant="secondary" onClick={onStart} disabled={busy}>
          Empezar
        </Button>
        <Button variant="quiet" className="text-sm" onClick={dismiss} disabled={busy}>
          Ahora no
        </Button>
      </div>
    </section>
  );
}

/**
 * "Ya hay 10 preguntas…": se puede practicar mientras se preparan las demás clases. Con el tope de
 * pedidos del día (`untilTomorrow`), dice que las demás se preparan mañana.
 */
function ReadyLine({
  ready,
  untilTomorrow,
  onPractice,
}: {
  ready: NonNullable<HomeData["ready"]>;
  untilTomorrow: boolean;
  onPractice: () => void;
}) {
  const course = tidy(ready.course);
  return (
    <p className="mt-1 text-sm text-muted">
      <span>
        {untilTomorrow
          ? `Ya hay ${ready.questions} preguntas de ${course}. Las demás clases se preparan mañana.`
          : `Ya hay ${ready.questions} preguntas de ${course}: podés empezar mientras se preparan las demás.`}
      </span>{" "}
      <button
        type="button"
        onClick={onPractice}
        className="press cursor-pointer text-ink underline decoration-line underline-offset-4 hover:decoration-ink"
      >
        Practicar
      </button>
    </p>
  );
}

function daysLeft(days: number): string {
  if (days === 0) return "Es hoy";
  if (days === 1) return "Falta 1 día";
  return `Faltan ${days} días`;
}

/**
 * Una sola vez, bajo el título, con la fecha del examen que muestra la pantalla:
 * "Probabilidad de que lo sepas el día del parcial (14/3)". Sin examen, "hoy".
 */
function probabilityTitle(at: number | null): string {
  return `Probabilidad de que lo sepas ${at == null ? "hoy" : `el día del parcial (${shortDate(at)})`}`;
}

/**
 * Cada número es la probabilidad al día del examen más próximo de ese tema (`topic.at`). Si no es
 * el del encabezado (el tema entra antes en otro examen, o en ninguno), lleva su propia fecha.
 */
function TopicRow({ topic, headerAt, onClick }: { topic: Topic; headerAt: number | null; onClick: () => void }) {
  const percent = Math.round(topic.mastery * 100);
  const ownDate = topic.at === headerAt ? "" : ` · ${topic.at == null ? "hoy" : shortDate(topic.at)}`;
  return (
    <li className="border-b border-line">
      <button
        type="button"
        onClick={onClick}
        title={`Practicar ${topic.name}`}
        className="group -mx-2 block w-[calc(100%+1rem)] cursor-pointer px-2 py-3 text-left"
      >
        <span className="flex items-baseline justify-between gap-6">
          <span className="min-w-0 group-hover:underline group-hover:decoration-line group-hover:underline-offset-4">
            {topic.name}
          </span>
          {topic.total > 0 && <span className="shrink-0 text-sm tabular-nums text-muted">{`${percent} %${ownDate}`}</span>}
        </span>
        {topic.total > 0 ? (
          <>
            <span className="mt-2 block h-1 overflow-hidden rounded-full bg-track" aria-hidden>
              <span
                data-testid="mastery-bar"
                className="block h-full rounded-full bg-ink/75"
                // Mismo número que el texto: la barra llega justo al porcentaje que se muestra.
                style={{ width: `${percent}%` }}
              />
            </span>
            {topic.seen < Math.min(FEW_SEEN, topic.total) && (
              <span className="mt-1.5 block text-sm tabular-nums text-muted">{`Viste ${topic.seen} de ${topic.total} preguntas`}</span>
            )}
          </>
        ) : (
          <span className="mt-1 block text-sm text-muted">Todavía no tiene preguntas</span>
        )}
      </button>
    </li>
  );
}

/** Lo de hoy: la próxima clase y la sesión del plan. */
function Today({
  items,
  now,
  onPractice,
}: {
  items: AgendaItem[];
  now: number;
  onPractice: (params: QuizParams, session?: SessionInfo) => void;
}) {
  const nextClass = items.find((i) => i.kind === "class" && (i.ends_at ?? i.starts_at) > now);
  const session = items.find((i) => i.kind === "session" && !i.done && (i.ends_at ?? i.starts_at) > now);
  if (!nextClass && !session) return null;
  const classRow = nextClass && (
    <li key="class" className="text-muted">
      <span className="tabular-nums text-ink">{clock(nextClass.starts_at)}</span>
      {" · "}
      {[tidy(nextClass.course ?? nextClass.title), nextClass.place].filter(Boolean).join(" · ")}
    </li>
  );
  const sessionRow = session && (
    <li key="session" className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <span className="text-muted">
        <span className="tabular-nums text-ink">{clock(session.starts_at)}</span> · {session.title}
      </span>
      <Button
        variant="quiet"
        aria-label={`Practicar ${session.title}`}
        onClick={() =>
          onPractice(
            { session_id: session.id },
            {
              kind: session.session_kind ?? "focus",
              topic: session.topic ?? "",
              review_minutes: session.review_minutes ?? 0,
              focus_minutes: session.focus_minutes ?? 0,
            },
          )
        }
      >
        Practicar
      </Button>
    </li>
  );
  // En el orden en que pasan.
  const classFirst = !session || (nextClass != null && nextClass.starts_at <= session.starts_at);
  return (
    <section className="mb-10">
      <h2 className="mb-3 font-semibold">Hoy</h2>
      <ul className="space-y-2">{classFirst ? [classRow, sessionRow] : [sessionRow, classRow]}</ul>
    </section>
  );
}

/** Después de un examen: "¿Cómo te fue?", con la nota o "Prefiero no decir". */
function ResultPrompt({ exam, onDone }: { exam: ExamInfo; onDone: (change: Partial<ExamInfo>) => void }) {
  const id = useId();
  const scale = exam.max_grade ?? 10;
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    const number = Number(value.replace(",", ".").trim());
    if (!value.trim() || !Number.isFinite(number) || number < 0 || number > scale) {
      setError(`La nota tiene que estar entre 0 y ${grade(scale)}.`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onDone({ result: await api.examResult(exam.id, number) });
    } catch {
      setError("No se pudo guardar. Probá de nuevo.");
      setBusy(false);
    }
  }

  async function skip() {
    setBusy(true);
    try {
      await api.skipExamResult(exam.id);
      onDone({ result_skipped: true });
    } catch {
      setError("No se pudo guardar. Probá de nuevo.");
      setBusy(false);
    }
  }

  return (
    <section className="mb-12 border-b border-line pb-8">
      <h2 className="mb-1 font-semibold">¿Cómo te fue en {tidy(exam.name)}?</h2>
      <p className="mb-4 text-sm text-muted">Con tu nota real, Estudia aprende cuánto se parecen sus simulacros al examen.</p>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <div>
          <label htmlFor={id} className="mb-1.5 block text-sm font-medium">
            Nota, de 0 a {grade(scale)}
          </label>
          <input
            id={id}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              setError(null);
            }}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `${id}-err` : undefined}
            className={`w-28 tabular-nums ${FIELD}`}
          />
        </div>
        <Button type="submit" variant="secondary" disabled={busy}>
          Guardar nota
        </Button>
        <Button variant="quiet" className="mb-2" onClick={skip} disabled={busy}>
          Prefiero no decir
        </Button>
      </form>
      {error && (
        <p id={`${id}-err`} role="alert" className="mt-2 text-sm text-bad">
          {error}
        </p>
      )}
    </section>
  );
}

function Processing({
  processing,
  stopReason,
  working,
  age,
  saidByReady = false,
}: {
  processing: HomeData["processing"];
  stopReason: AppState["ai_stop_reason"];
  working: boolean;
  age?: Age | null;
  saidByReady?: boolean;
}) {
  const { pending, done, failed } = processing;
  if (age === "minor") {
    return (
      <p className="mt-10 text-sm text-muted">
        Sin IA no se preparan clases nuevas. Podés practicar con las preguntas que ya hay.
      </p>
    );
  }
  if (!pending && !done && !failed) return null;
  let line: string | null = null;
  // Sin responder la edad no se prepara nada: el aviso de arriba ya lo explica.
  if (age === null && pending) line = null;
  else if (!pending) line = done ? plural(done, "clase lista", "clases listas") : null;
  // Con la cuota agotada, el aviso de arriba ya lo dice.
  else if (stopReason === "quota" || stopReason === "youtube_cap") line = null;
  // El tope de pedidos del día no es un error: es información tranquila.
  else if (stopReason === "budget") line = saidByReady ? null : "Lo de hoy ya está. Las demás clases siguen mañana.";
  else if (stopReason === "unavailable") line = `${plural(pending, "clase", "clases")} en espera`;
  // "Preparando" solo si de verdad hay algo corriendo; si no, no se promete avance.
  else if (working) line = `Preparando ${plural(pending, "clase", "clases")}`;
  else line = `${plural(pending, "clase", "clases")} por preparar`;
  if (!line && !failed) return null;
  return (
    <div className="mt-10 space-y-1 text-sm text-muted">
      {line && <p>{line}</p>}
      {failed > 0 && <p>{plural(failed, "clase no se pudo procesar", "clases no se pudieron procesar")}</p>}
    </div>
  );
}
