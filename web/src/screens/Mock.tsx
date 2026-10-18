import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ApiError, api, type ExamInfo, type MockAnswer, type MockResult, type MockStart, type QuizParams } from "../api";
import { formatDuration, formatTimestamp, grade, keepMath, tidy } from "../format";
import { Button, FIELD, Label, Notice, TypedField } from "../ui";
import { OptionButton } from "./Practice";

/** Si el examen no dice cuánto dura, el servidor da 90 segundos por pregunta. */
const PER_QUESTION_TEXT = "90 segundos por pregunta";

interface Props {
  onPractice: (params: QuizParams) => void;
  onExit: () => void;
}

type Answers = Record<number, MockAnswer>;

export default function Mock({ onPractice, onExit }: Props) {
  const [exams, setExams] = useState<ExamInfo[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [examId, setExamId] = useState<number | null>(null);
  const [attempt, setAttempt] = useState<MockStart | null>(null);
  const [result, setResult] = useState<MockResult | null>(null);
  const [answers, setAnswers] = useState<Answers>({});

  useEffect(() => {
    let alive = true;
    api
      .exams()
      .then((list) => {
        if (!alive) return;
        const now = Date.now() / 1000;
        const upcoming = list.filter((e) => e.at != null && (e.closes_at ?? e.at) > now);
        setExams(upcoming);
        setExamId(upcoming[0]?.id ?? null);
      })
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, []);

  if (failed) return <Notice>No se pudieron cargar los exámenes. Probá recargar la página.</Notice>;
  if (!exams) return null;
  const exam = exams.find((e) => e.id === examId) ?? null;

  if (result && attempt && exam) {
    return (
      <Result
        exam={exam}
        attempt={attempt}
        answers={answers}
        result={result}
        onPractice={onPractice}
        onExit={onExit}
      />
    );
  }
  if (attempt) {
    return (
      <Running
        attempt={attempt}
        answers={answers}
        setAnswers={setAnswers}
        onFinished={setResult}
      />
    );
  }
  return (
    <Setup
      exams={exams}
      exam={exam}
      onChoose={setExamId}
      onStarted={(a) => {
        setAnswers({});
        setAttempt(a);
      }}
      onExit={onExit}
    />
  );
}

// --- Antes de empezar --------------------------------------------------------------------------

function Setup({
  exams,
  exam,
  onChoose,
  onStarted,
  onExit,
}: {
  exams: ExamInfo[];
  exam: ExamInfo | null;
  onChoose: (id: number) => void;
  onStarted: (attempt: MockStart) => void;
  onExit: () => void;
}) {
  const id = useId();
  const [prediction, setPrediction] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!exam) {
    return (
      <div className="max-w-xl">
        <h1 className="mb-3 font-serif text-[2rem] leading-tight tracking-[-0.02em]">Simulacro</h1>
        <p className="mb-8 text-muted">No hay exámenes próximos. Podés agregar uno desde la Agenda.</p>
        <Button variant="secondary" onClick={onExit}>
          Volver al inicio
        </Button>
      </div>
    );
  }

  const scale = exam.max_grade ?? 10;

  async function start() {
    let predicted: number | null = null;
    if (prediction.trim()) {
      predicted = Number(prediction.replace(",", ".").trim());
      if (!Number.isFinite(predicted) || predicted < 0 || predicted > scale) {
        setError(`La nota va de 0 a ${grade(scale)}. Si no sabés, dejalo vacío.`);
        return;
      }
    }
    setBusy(true);
    setError(null);
    try {
      onStarted(await api.startMock(exam!.id, predicted));
    } catch (err) {
      setError(err instanceof ApiError && err.status === 409 ? err.detail : "No se pudo armar el simulacro. Probá de nuevo.");
      setBusy(false);
    }
  }

  return (
    <div className="max-w-xl">
      <Label className="mb-2">Simulacro</Label>
      {exams.length > 1 ? (
        <fieldset className="mb-6">
          <legend className="mb-3 font-serif text-[2rem] leading-tight tracking-[-0.02em]">¿De qué examen?</legend>
          <div className="space-y-1">
            {exams.map((e) => (
              <label key={e.id} className="flex cursor-pointer items-baseline gap-3 py-1">
                <input
                  type="radio"
                  name={`${id}-exam`}
                  checked={e.id === exam.id}
                  onChange={() => onChoose(e.id)}
                  className="accent-[var(--ink)]"
                />
                <span>
                  {tidy(e.name)} <span className="text-muted">· {tidy(e.course)}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
      ) : (
        <h1 className="mb-2 font-serif text-[2.2rem] leading-[1.1] tracking-[-0.025em]">{tidy(exam.name)}</h1>
      )}
      <p className="text-muted">
        {[
          // Con el selector a la vista, la materia ya está en cada opción.
          exams.length > 1 ? null : tidy(exam.course),
          exam.time_limit_s ? formatDuration(exam.time_limit_s) : PER_QUESTION_TEXT,
        ]
          .filter(Boolean)
          .join(" · ")}
      </p>
      <p className="mt-6 font-medium">No vas a ver las respuestas hasta el final</p>
      <p className="mt-1 text-sm text-muted">Podés ir y volver entre las preguntas. Al terminar el tiempo se entrega solo.</p>

      <form
        className="mt-10"
        onSubmit={(e) => {
          e.preventDefault();
          start();
        }}
      >
        <label htmlFor={`${id}-pred`} className="mb-1.5 block font-medium">
          ¿Cuánto creés que te sacás?
        </label>
        <p id={`${id}-pred-help`} className="mb-2 text-sm text-muted">
          De 0 a {grade(scale)}. Si no sabés, dejalo vacío.
        </p>
        <input
          id={`${id}-pred`}
          type="text"
          inputMode="decimal"
          autoComplete="off"
          value={prediction}
          onChange={(e) => {
            setPrediction(e.target.value);
            setError(null);
          }}
          aria-invalid={error ? true : undefined}
          aria-describedby={`${id}-pred-help`}
          className={`w-28 tabular-nums ${FIELD}`}
        />
        {error && (
          <p role="alert" className="mt-2 text-sm text-bad">
            {error}
          </p>
        )}
        <div className="mt-8 flex flex-wrap gap-3">
          <Button type="submit" disabled={busy}>
            Empezar
          </Button>
          <Button variant="quiet" onClick={onExit}>
            Volver
          </Button>
        </div>
      </form>
    </div>
  );
}

// --- Durante -----------------------------------------------------------------------------------

function Running({
  attempt,
  answers,
  setAnswers,
  onFinished,
}: {
  attempt: MockStart;
  answers: Answers;
  setAnswers: (update: (a: Answers) => Answers) => void;
  onFinished: (result: MockResult) => void;
}) {
  const [index, setIndex] = useState(0);
  const deadline = useRef(Date.now() + attempt.seconds * 1000);
  const [left, setLeft] = useState(attempt.seconds);
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);
  const [timeUp, setTimeUp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Entregando o con el tiempo vencido: ya no se responde, aunque la entrega falle. */
  const locked = sending || timeUp;
  const answersRef = useRef(answers);
  answersRef.current = answers;
  const sentRef = useRef(false);

  const items = attempt.items;
  const item = items[index];

  const finish = useCallback(async () => {
    if (sentRef.current) return;
    sentRef.current = true;
    setSending(true);
    setError(null);
    // Las escritas en blanco no se mandan: cuentan como sin responder.
    const body: Answers = {};
    for (const [qid, answer] of Object.entries(answersRef.current)) {
      if ("typed" in answer && !answer.typed.trim()) continue;
      body[Number(qid)] = answer;
    }
    try {
      onFinished(await api.finishMock(attempt.attempt_id, body));
    } catch (err) {
      // Ya se había entregado (por ejemplo, un reintento después de un corte): se muestra lo guardado.
      if (err instanceof ApiError && err.status === 409) {
        try {
          onFinished(await api.mockResult(attempt.attempt_id));
          return;
        } catch {
          // sigue abajo: se avisa y se puede reintentar
        }
      }
      sentRef.current = false;
      setSending(false);
      setError(
        err instanceof ApiError && err.status === 503
          ? "No pude corregir ahora, probá de nuevo."
          : "No se pudo entregar. Probá de nuevo.",
      );
    }
  }, [attempt.attempt_id, onFinished]);

  // Reloj: al llegar a cero se entrega solo.
  useEffect(() => {
    const timer = window.setInterval(() => {
      const remaining = Math.max(0, Math.ceil((deadline.current - Date.now()) / 1000));
      setLeft(remaining);
      if (remaining === 0) {
        window.clearInterval(timer);
        setTimeUp(true);
        finish();
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [finish]);

  const choose = useCallback(
    (option: number) => setAnswers((a) => ({ ...a, [item.question_id]: { index: option } })),
    [item.question_id, setAnswers],
  );

  // Teclado: 1–4 eligen la opción.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey || locked) return;
      if (event.target instanceof HTMLElement && event.target.closest("input, textarea, select")) return;
      const n = Number(event.key);
      if (item.format !== "typed" && n >= 1 && n <= item.options.length) {
        event.preventDefault();
        choose(n - 1);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [item, choose, locked]);

  const answered = (qid: number) => {
    const a = answers[qid];
    return a !== undefined && !("typed" in a && !a.typed.trim());
  };
  const unanswered = items.filter((q) => !answered(q.question_id)).length;
  const current = answers[item.question_id];

  return (
    <div className="max-w-2xl">
      <div className="mb-4 flex items-baseline justify-between gap-6">
        <Label>
          Simulacro · pregunta {index + 1} de {items.length}
        </Label>
        <span
          role="timer"
          aria-label="Tiempo restante"
          className={`text-sm tabular-nums ${left <= 60 ? "font-medium text-ink" : "text-muted"}`}
        >
          {formatTimestamp(left)}
        </span>
      </div>

      <h1 className="math mb-7 text-[1.45rem] leading-snug tracking-[-0.01em]">{keepMath(item.stem)}</h1>

      {item.format === "typed" ? (
        <div className="max-w-xl">
          <TypedField
            key={item.question_id}
            value={current && "typed" in current ? current.typed : ""}
            onChange={(typed) => setAnswers((a) => ({ ...a, [item.question_id]: { typed } }))}
            disabled={locked}
          />
        </div>
      ) : (
        <ol className="space-y-2">
          {item.options.map((option, i) => (
            <li key={i}>
              <OptionButton
                text={keepMath(option)}
                number={i + 1}
                state={current && "index" in current && current.index === i ? "picked" : "idle"}
                disabled={locked}
                onClick={() => choose(i)}
              />
            </li>
          ))}
        </ol>
      )}

      {index + 1 < items.length && (
        <div className="mt-8">
          <Button variant="secondary" onClick={() => setIndex((i) => i + 1)}>
            Siguiente
          </Button>
        </div>
      )}

      <nav aria-label="Preguntas" className="mt-8 flex flex-wrap gap-1.5">
        {items.map((q, i) => (
          <button
            key={q.question_id}
            type="button"
            onClick={() => setIndex(i)}
            aria-label={`Ir a la pregunta ${i + 1}${answered(q.question_id) ? ", respondida" : ""}`}
            aria-current={i === index ? "step" : undefined}
            className={`press size-8 cursor-pointer rounded-md border text-sm tabular-nums ${
              answered(q.question_id) ? "bg-ink/10 text-ink" : "text-muted hover:bg-hover"
            } ${i === index ? "border-ink font-medium" : "border-transparent"}`}
          >
            {i + 1}
          </button>
        ))}
      </nav>

      <div className="mt-10 border-t border-line pt-6">
        {confirming ? (
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-3">
            <p>
              ¿Terminar ahora?
              {unanswered > 0 && (
                <span className="text-muted">
                  {" "}
                  {unanswered === 1 ? "Queda 1 sin responder." : `Quedan ${unanswered} sin responder.`}
                </span>
              )}
            </p>
            <Button onClick={finish} disabled={sending}>
              Sí, terminar
            </Button>
            <Button variant="quiet" onClick={() => setConfirming(false)} disabled={locked}>
              Seguir
            </Button>
          </div>
        ) : (
          <Button variant="quiet" onClick={() => setConfirming(true)} disabled={locked}>
            Terminar
          </Button>
        )}
        {sending && <p className="mt-3 text-sm text-muted">Corrigiendo…</p>}
        {error && (
          <p role="alert" className="mt-3 text-sm text-bad">
            {error}{" "}
            <Button variant="quiet" onClick={finish} disabled={sending}>
              Entregar de nuevo
            </Button>
          </p>
        )}
      </div>
    </div>
  );
}

// --- Resultado ---------------------------------------------------------------------------------

function Result({
  exam,
  attempt,
  answers,
  result,
  onPractice,
  onExit,
}: {
  exam: ExamInfo;
  attempt: MockStart;
  answers: Answers;
  result: MockResult;
  onPractice: (params: QuizParams) => void;
  onExit: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // La revisión se arma por pregunta: si alguna se retiró durante el intento, no está y se ven las demás.
  const byId = new Map(result.review.map((fb) => [fb.question_id, fb]));
  const reviewed = attempt.items.filter((item) => byId.has(item.question_id));

  async function practiceFailed() {
    setBusy(true);
    setError(null);
    try {
      const topics = await api.examTopics(exam.id);
      const ids = topics.filter((t) => result.failed_topics.includes(t.name)).map((t) => t.id);
      onPractice(ids.length ? { topic_ids: ids } : {});
    } catch {
      setError("No se pudo armar la práctica. Probá de nuevo.");
      setBusy(false);
    }
  }

  return (
    <div className="reveal max-w-2xl">
      <Label className="mb-2">Simulacro · {tidy(exam.name)}</Label>
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
        <h1 className="font-serif text-[3.2rem] leading-none tracking-[-0.03em] tabular-nums">
          {grade(result.grade)}
          <span className="ml-2 text-[1.4rem] tracking-normal text-muted">de {grade(result.max_grade)}</span>
        </h1>
        {result.predicted_grade != null && (
          <p className="text-muted">Creías que te sacabas {grade(result.predicted_grade)}</p>
        )}
      </div>
      <p className="mt-4 font-medium tabular-nums">
        {result.correct} de {result.total} bien
      </p>
      {result.failed_topics.length > 0 && <p className="mt-1">Fallaste en: {result.failed_topics.join(", ")}</p>}
      {result.late && <p className="mt-1 text-sm text-muted">Se entregó después de la hora.</p>}
      {result.correct < result.total && (
        <p className="mt-4 text-muted">Tus errores vuelven mañana en la práctica.</p>
      )}
      <div className="mt-8 flex flex-wrap gap-3">
        {result.failed_topics.length > 0 && (
          <Button onClick={practiceFailed} disabled={busy}>
            Practicar lo que fallé
          </Button>
        )}
        <Button variant={result.failed_topics.length ? "secondary" : "primary"} onClick={onExit}>
          Volver al inicio
        </Button>
      </div>
      {error && (
        <p role="alert" className="mt-3 text-sm text-bad">
          {error}
        </p>
      )}

      {reviewed.length > 0 && (
        <section className="mt-16">
          <h2 className="mb-4 font-semibold">Revisión</h2>
          <ol>
            {reviewed.map((item, i) => {
              const fb = byId.get(item.question_id)!;
              const given = answers[item.question_id];
              let yours: string | null = null;
              if (given && "index" in given) yours = item.options[given.index];
              else if (given && "typed" in given && given.typed.trim()) yours = given.typed;
              const why = (
                <>
                  <p className="math whitespace-pre-line leading-relaxed">{keepMath(fb.explanation)}</p>
                  {!fb.correct && fb.common_error && (
                    <p className="math mt-2 leading-relaxed">
                      <span className="font-semibold">Error común:</span> {keepMath(fb.common_error)}
                    </p>
                  )}
                </>
              );
              return (
                <li key={item.question_id} className="border-t border-line py-6">
                  <div className="mb-2 flex items-baseline justify-between gap-4">
                    <span className="text-sm text-muted tabular-nums">
                      {i + 1} · {item.topic}
                    </span>
                    <span className={`text-sm font-medium ${fb.correct ? "text-ok" : "text-bad"}`}>
                      {fb.correct ? "Bien" : "Mal"}
                    </span>
                  </div>
                  <p className="math mb-3 text-[1.0625rem] leading-snug">{keepMath(item.stem)}</p>
                  <p className="math text-sm font-medium">
                    Correcta: {keepMath(item.options[fb.correct_index])}
                  </p>
                  <p className="math text-sm text-muted">
                    Tu respuesta: {yours === null ? "sin responder" : keepMath(yours)}
                    {!fb.correct && yours !== null && <span className="text-bad"> (incorrecta)</span>}
                  </p>
                  {fb.correct ? (
                    <details className="group mt-3">
                      <summary className="w-fit cursor-pointer list-none text-sm text-muted underline decoration-line underline-offset-4 hover:text-ink [&::-webkit-details-marker]:hidden">
                        <span className="group-open:hidden">Ver por qué</span>
                        <span className="hidden group-open:inline">Ocultar</span>
                      </summary>
                      <div className="mt-3">{why}</div>
                    </details>
                  ) : (
                    <div className="mt-3">{why}</div>
                  )}
                </li>
              );
            })}
          </ol>
        </section>
      )}
    </div>
  );
}
