import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import {
  ApiError,
  api,
  type AnswerMode,
  type Confidence,
  type Feedback,
  type QuizItem,
  type QuizParams,
  type SessionInfo,
} from "../api";
import { formatTimestamp, keepMath, plural, tidy } from "../format";
import { Button, DocumentSource, FIELD, Label, Notice, TypedField } from "../ui";

const UNDO_MS = 5000;
/** Enter no saltea la corrección antes de este tiempo: primero hay que leerla. */
export const FEEDBACK_MIN_MS = 1500;

const RELEARN_NOTE = "La sabés cuando la acertás en tres días distintos.";
const MIX_NOTE =
  "Mezclamos temas a propósito: se siente más difícil, pero en el parcial vas a tener que reconocer qué método va.";

interface Props {
  params: QuizParams;
  /** Si la práctica es una sesión del plan: para el encabezado de cada fase. */
  session?: SessionInfo;
  onExit: () => void;
}

/** Pregunta con sus opciones en el orden en que se muestran (`order[i]` = índice original). */
interface Shown extends QuizItem {
  order: number[];
  /** Distingue la misma pregunta cuando vuelve al final de la sesión. */
  key: number;
}

interface Result {
  item: Shown;
  correct: boolean;
}

const CONFIDENCE: { value: Confidence; label: string; key: string }[] = [
  { value: 2, label: "Seguro", key: "S" },
  { value: 1, label: "Me parece", key: "M" },
  { value: 0, label: "Adiviné", key: "A" },
];

let nextKey = 0;

export function shuffled(length: number): number[] {
  const order = Array.from({ length }, (_, i) => i);
  for (let i = order.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [order[i], order[j]] = [order[j], order[i]];
  }
  return order;
}

/** Cada intento mezcla las opciones de nuevo, también al repetir lo fallado. */
function withShuffledOptions(quiz: QuizItem[]): Shown[] {
  return quiz.map((q) => ({ ...q, order: shuffled(q.options.length), key: nextKey++ }));
}

/** Avisos que se explican una sola vez. Sin almacenamiento, se muestran cada vez. */
function firstTime(name: string): boolean {
  try {
    return window.localStorage.getItem(`estudia.seen.${name}`) === null;
  } catch {
    return true;
  }
}

function markSeen(name: string) {
  try {
    window.localStorage.setItem(`estudia.seen.${name}`, "1");
  } catch {
    // sin almacenamiento: no pasa nada
  }
}

export default function Practice({ params, session, onExit }: Props) {
  const [items, setItems] = useState<Shown[] | null>(null);
  const [index, setIndex] = useState(0);
  /** Las opciones están a la vista (en "pensalo primero" y en las escritas, recién al pedirlas). */
  const [revealed, setRevealed] = useState(true);
  /** Índice original de la opción marcada, esperando la confianza. */
  const [chosen, setChosen] = useState<number | null>(null);
  const [typed, setTyped] = useState("");
  /** "Corregir" ya se tocó: falta la confianza. */
  const [typedReady, setTypedReady] = useState(false);
  const [typedError, setTypedError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [answeredText, setAnsweredText] = useState<string | null>(null);
  const [canSkip, setCanSkip] = useState(false);
  const [relearnNote, setRelearnNote] = useState(false);
  const [mixNote, setMixNote] = useState(false);
  const [results, setResults] = useState<Result[]>([]);
  const [reported, setReported] = useState<number[]>([]);
  const [undo, setUndo] = useState<Shown | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [classTitles, setClassTitles] = useState<Map<string, string>>(new Map());
  /**
   * "Explicá por qué" después de un acierto dudoso: `ask` mientras escribe (la explicación espera),
   * `compare` con lo escrito a la vista, `done` cuando ya contestó o salteó.
   */
  const [explain, setExplain] = useState<{ step: "ask" | "compare" | "done"; text: string } | null>(null);
  const shownAt = useRef(Date.now());
  const revealedMs = useRef<number | null>(null);
  const feedbackRef = useRef<HTMLElement>(null);
  const confidenceRef = useRef<HTMLDivElement>(null);
  const typedRef = useRef<HTMLInputElement>(null);

  const item = items?.[index];
  const finished = items !== null && index >= items.length;
  const typedMode = item?.format === "typed" && !revealed;

  const start = useCallback((quiz: QuizItem[]) => {
    const shown = withShuffledOptions(quiz);
    setItems(shown);
    setIndex(0);
    setFeedback(null);
    setResults([]);
    setReported([]);
    setUndo(null);
    if (shown.some((q) => q.phase === "review") && firstTime("mix")) {
      setMixNote(true);
      markSeen("mix");
    }
  }, []);

  useEffect(() => {
    let alive = true;
    setItems(null);
    setError(null);
    api
      .quiz(params)
      .then((quiz) => alive && start(quiz))
      .catch(() => alive && setError("No se pudo armar la práctica. Probá de nuevo."));
    return () => {
      alive = false;
    };
  }, [params, start]);

  // Nombres de las clases, para citar la fuente de cada pregunta.
  useEffect(() => {
    let alive = true;
    api
      .materials()
      .then((sections) => {
        if (!alive) return;
        const titles = new Map<string, string>();
        for (const section of sections)
          for (const m of section.materials) if (m.youtube_id) titles.set(m.youtube_id, tidy(m.title));
        setClassTitles(titles);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // Cada pregunta nueva empieza de cero (en el mismo render, para que no se vean las opciones
  // de "pensalo primero" ni un instante): opciones según el formato, nada marcado, reloj en cero.
  const [stateFor, setStateFor] = useState<number | undefined>(undefined);
  if (item?.key !== stateFor) {
    setStateFor(item?.key);
    setRevealed(item?.format !== "think_first" && item?.format !== "typed");
    setChosen(null);
    setTyped("");
    setTypedReady(false);
    setTypedError(null);
    setAnsweredText(null);
  }
  const itemKey = item?.key;
  useEffect(() => {
    shownAt.current = Date.now();
    revealedMs.current = null;
  }, [itemKey]);

  // El aviso de "Deshacer" dura unos segundos.
  useEffect(() => {
    if (!undo) return;
    const timer = window.setTimeout(() => setUndo(null), UNDO_MS);
    return () => window.clearTimeout(timer);
  }, [undo]);

  // La corrección se lee antes de seguir: Enter recién vale pasado un momento.
  useEffect(() => {
    setCanSkip(false);
    if (!feedback) return;
    // Si se pide "Explicá por qué", el foco va a su campo (lo enfoca el propio campo).
    if (!feedback.explain_prompt) feedbackRef.current?.focus();
    const timer = window.setTimeout(() => setCanSkip(true), FEEDBACK_MIN_MS);
    return () => window.clearTimeout(timer);
  }, [feedback]);

  const reveal = useCallback(() => {
    if (!item || revealed) return;
    revealedMs.current = Date.now() - shownAt.current;
    setRevealed(true);
    setTypedReady(false);
    setTypedError(null);
  }, [item, revealed]);

  const pick = useCallback(
    (original: number) => {
      if (!item || feedback || busy || !revealed) return;
      setChosen(original);
      setError(null);
    },
    [item, feedback, busy, revealed],
  );

  const checkTyped = useCallback(() => {
    if (!typed.trim()) return;
    setTypedError(null);
    setTypedReady(true);
    // El foco va a la confianza: así S, M o A responden desde el teclado.
    window.setTimeout(() => confidenceRef.current?.focus(), 0);
  }, [typed]);

  const send = useCallback(
    async (confidence: Confidence) => {
      if (!item || feedback || busy) return;
      const typing = item.format === "typed" && !revealed;
      if (typing ? !typedReady : chosen === null) return;
      let mode: AnswerMode = "mcq";
      if (item.format === "typed") mode = typing ? "typed" : "typed_then_mcq";
      else if (item.format === "think_first") mode = "mcq_think_first";
      setBusy(true);
      setError(null);
      try {
        const fb = await api.answer({
          question_id: item.question_id,
          chosen_index: typing ? null : item.order.indexOf(chosen!),
          typed_text: typing ? typed : null,
          confidence,
          mode,
          latency_ms: Date.now() - shownAt.current,
          options_revealed_ms: mode === "mcq_think_first" ? revealedMs.current : null,
          shown_order: typing ? [] : item.order,
        });
        setFeedback(fb);
        setExplain(fb.explain_prompt && fb.answer_id != null ? { step: "ask", text: "" } : null);
        setAnsweredText(typing ? typed : null);
        setResults((r) => (r.some((x) => x.item.question_id === item.question_id) ? r : [...r, { item, correct: fb.correct }]));
        if (fb.requeue) {
          // Vuelve al final de la sesión, con las opciones mezcladas de nuevo.
          setItems((list) => [...list!, ...withShuffledOptions([item])]);
        }
        if (fb.relearn_progress && firstTime("relearn")) {
          setRelearnNote(true);
          markSeen("relearn");
        } else {
          setRelearnNote(false);
        }
      } catch (err) {
        if (typing && err instanceof ApiError && (err.status === 400 || err.status === 503)) {
          // No se registró: se corrige el texto y se vuelve a intentar, sin gastar el intento.
          setTypedReady(false);
          setTypedError(
            err.status === 503
              ? "No pude corregir ahora, probá de nuevo."
              : err.detail.startsWith("No pude leer")
                ? `${err.detail}. Probá escribirlo de otra forma.`
                : `${err.detail}.`,
          );
          window.setTimeout(() => typedRef.current?.focus(), 0);
        } else {
          setError("No se pudo guardar la respuesta. Probá de nuevo.");
        }
      } finally {
        setBusy(false);
      }
    },
    [item, feedback, busy, revealed, typedReady, chosen, typed],
  );

  const next = useCallback(() => {
    setFeedback(null);
    setExplain(null);
    setError(null);
    setRelearnNote(false);
    setIndex((i) => i + 1);
  }, []);

  async function report() {
    if (!item || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.retire(item.question_id);
      // Sale de la ronda y no cuenta para el resultado; la siguiente queda en su lugar.
      setItems((list) => list!.filter((q) => q.question_id !== item.question_id));
      setResults((r) => r.filter((x) => x.item.question_id !== item.question_id));
      setReported((r) => [...r, item.question_id]);
      setFeedback(null);
      setUndo(item);
    } catch {
      setError("No se pudo reportar la pregunta. Probá de nuevo.");
    } finally {
      setBusy(false);
    }
  }

  async function undoReport() {
    if (!undo || busy) return;
    const back = undo;
    setBusy(true);
    try {
      await api.unretire(back.question_id);
      setReported((r) => r.filter((id) => id !== back.question_id));
      // Vuelve como pregunta actual, salvo que la actual ya esté respondida: entonces va después.
      const at = feedback ? index + 1 : index;
      setItems((list) => [...list!.slice(0, at), back, ...list!.slice(at)]);
      setUndo(null);
    } catch {
      setError("No se pudo deshacer. Probá de nuevo.");
    } finally {
      setBusy(false);
    }
  }

  // Teclado: 1–4 en el orden en que se ven las opciones, S, M o A para la confianza, Enter para seguir.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (!item || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target?.closest("input, textarea, select")) return;
      if (!feedback) {
        const n = Number(event.key);
        if (revealed && n >= 1 && n <= item.order.length) {
          event.preventDefault();
          pick(item.order[n - 1]);
          return;
        }
        const confidence = CONFIDENCE.find((c) => c.key === event.key.toUpperCase());
        const waiting = item.format === "typed" && !revealed ? typedReady : chosen !== null;
        if (confidence && waiting) {
          event.preventDefault();
          send(confidence.value);
        }
      } else if (event.key === "Enter" && !target?.closest("button, a, summary")) {
        event.preventDefault();
        // Mientras se pide "Explicá por qué", se sigue con Listo o Saltear.
        if (canSkip && explain?.step !== "ask") next();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [item, feedback, revealed, typedReady, chosen, canSkip, explain, pick, send, next]);

  if (items === null) return error ? <Notice>{error}</Notice> : null;

  const undoLine = undo && (
    <span role="status" className="text-sm text-muted">
      Listo, no te la vamos a mostrar más ·{" "}
      <button
        type="button"
        onClick={undoReport}
        disabled={busy}
        className="press cursor-pointer font-medium text-ink underline decoration-line underline-offset-4 hover:decoration-ink"
      >
        Deshacer
      </button>
    </span>
  );

  if (items.length === 0 && reported.length === 0) {
    return (
      <div className="max-w-xl">
        <h1 className="mb-3 font-serif text-[2rem] leading-tight tracking-[-0.02em]">Todavía no hay preguntas</h1>
        <p className="mb-8 text-muted">
          Las preguntas aparecen a medida que se procesan tus clases, y solo las que pasaron la verificación.
        </p>
        <Button variant="secondary" onClick={onExit}>
          Volver al inicio
        </Button>
      </div>
    );
  }

  if (finished) {
    const failed = results.filter((r) => !r.correct).map((r) => r.item);
    const failedTopics = [...new Set(failed.map((q) => q.topic))];
    const right = results.length - failed.length;
    return (
      <div className="reveal max-w-xl">
        {undoLine && <div className="mb-6">{undoLine}</div>}
        <Label className="mb-2">Terminaste</Label>
        {results.length ? (
          <h1 className="font-serif text-[2.6rem] leading-[1.1] tracking-[-0.025em] tabular-nums">
            {right} de {results.length} bien
          </h1>
        ) : (
          <h1 className="font-serif text-[2rem] leading-tight">No quedaron preguntas para responder</h1>
        )}
        {failedTopics.length > 0 && <p className="mt-3">Fallaste en: {failedTopics.join(", ")}</p>}
        {reported.length > 0 && (
          <p className="mt-1 text-muted">{plural(reported.length, "pregunta reportada", "preguntas reportadas")}</p>
        )}
        <p className="mt-6 mb-8 text-muted">
          {failed.length
            ? "Lo que fallaste vuelve en los próximos días hasta que la aciertes en tres días distintos."
            : results.length
              ? "Todas bien."
              : ""}
        </p>
        <div className="flex flex-wrap gap-3">
          {failed.length > 0 && <Button onClick={() => start(failed)}>Practicar lo que fallé</Button>}
          <Button variant={failed.length ? "secondary" : "primary"} onClick={onExit}>
            Volver al inicio
          </Button>
        </div>
      </div>
    );
  }

  const current = item!;
  const number = `pregunta ${index + 1} de ${items.length}`;
  const phase = phaseTitle(current, session);
  const waitingConfidence = !feedback && (typedMode ? typedReady : chosen !== null);

  return (
    <div className="max-w-2xl">
      {phase && <p className="mb-2 text-sm font-medium">{phase}</p>}
      {mixNote && current.phase === "review" && <p className="mb-5 max-w-xl text-sm text-muted">{MIX_NOTE}</p>}
      <Label className="mb-4">
        {current.topic} · <span className="whitespace-nowrap">{number}</span>
      </Label>

      <h1 className="math mb-7 text-[1.45rem] leading-snug tracking-[-0.01em]">{keepMath(current.stem)}</h1>

      {!revealed && current.format === "think_first" && (
        <div>
          <p className="mb-3 text-sm text-muted">Pensá la respuesta antes de ver las opciones.</p>
          <Button variant="secondary" onClick={reveal}>
            Ver opciones
          </Button>
        </div>
      )}

      {typedMode && !feedback && (
        <div className="max-w-xl">
          <TypedField
            value={typed}
            onChange={(value) => {
              setTyped(value);
              setTypedReady(false);
              setTypedError(null);
            }}
            onSubmit={checkTyped}
            // Con la confianza a la vista, "Corregir" ya se usó; vuelve si cambia lo escrito.
            submitLabel={typedReady ? undefined : "Corregir"}
            error={typedError}
            disabled={busy}
            inputRef={typedRef}
          />
          <Button variant="quiet" className="mt-4 text-sm" onClick={reveal} disabled={busy}>
            Mostrame opciones
          </Button>
        </div>
      )}

      {revealed && (
        <ol className="space-y-2">
          {current.order.map((original, shownAt) => (
            <li key={original}>
              <OptionButton
                text={keepMath(current.options[original])}
                number={shownAt + 1}
                state={optionState(original, chosen, feedback)}
                disabled={feedback !== null || busy}
                onClick={() => pick(original)}
              />
            </li>
          ))}
        </ol>
      )}

      {waitingConfidence && (
        <div
          ref={confidenceRef}
          tabIndex={-1}
          role="group"
          aria-label="¿Qué tan seguro estás?"
          className="mt-6 outline-none"
        >
          <p className="mb-2 text-sm text-muted">¿Qué tan seguro estás?</p>
          <div className="flex flex-wrap gap-2">
            {CONFIDENCE.map((c) => (
              <Button
                key={c.value}
                variant="secondary"
                onClick={() => send(c.value)}
                disabled={busy}
                className="flex items-baseline gap-2"
              >
                {c.label}
                <kbd className="font-sans text-xs font-normal text-faint [@media(pointer:coarse)]:hidden" aria-hidden>
                  {c.key}
                </kbd>
              </Button>
            ))}
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error}
        </p>
      )}

      {!feedback && (
        <div className="mt-6 text-sm">
          {undoLine ?? (
            <Button variant="quiet" onClick={report} disabled={busy}>
              Reportar un error
            </Button>
          )}
        </div>
      )}

      {feedback && (
        <section ref={feedbackRef} tabIndex={-1} className="reveal mt-8 border-t border-line pt-6 outline-none">
          {answeredText !== null && (
            <p className="math mb-4">
              Escribiste {keepMath(answeredText)}:{" "}
              <span className={`font-semibold ${feedback.correct ? "text-ok" : "text-bad"}`}>
                {feedback.correct ? "es correcta" : "no es correcta"}
              </span>
            </p>
          )}
          {explain?.step === "ask" ? (
            <ExplainPrompt
              onDone={(text) => setExplain({ step: text ? "compare" : "done", text })}
            />
          ) : (
          <FeedbackBody
            feedback={feedback}
            typed={answeredText !== null}
            correctText={current.options[feedback.correct_index]}
            source={
              feedback.source_label ? (
                <DocumentSource className="mt-4" label={feedback.source_label} url={feedback.source_url} />
              ) : (
              feedback.youtube_id && (
                <p className="mt-4 text-sm">
                  <a
                    href={sourceUrl(feedback.youtube_id, feedback.source_seconds)}
                    target="_blank"
                    rel="noreferrer"
                    className="underline decoration-line underline-offset-4 hover:decoration-ink"
                  >
                    {sourceText(classTitles.get(feedback.youtube_id), feedback.source_seconds)}
                  </a>
                </p>
              )
              )
            }
          />
          )}
          {explain && explain.step !== "ask" && explain.text && feedback.answer_id != null && (
            <ExplainCompare text={explain.text} answerId={feedback.answer_id} />
          )}
          {explain?.step !== "ask" && (feedback.next_label || feedback.relearn_progress) && (
            <p className="mt-6 text-sm text-muted">
              {[feedback.next_label, feedback.relearn_progress].filter(Boolean).join(" · ")}
            </p>
          )}
          {relearnNote && explain?.step !== "ask" && <p className="mt-1 text-sm text-muted">{RELEARN_NOTE}</p>}
          <div className={`mt-8 flex flex-wrap items-baseline gap-x-6 gap-y-3 ${explain?.step === "ask" ? "hidden" : ""}`}>
            <Button onClick={next}>{index + 1 < items.length ? "Siguiente" : "Ver resultado"}</Button>
            {undoLine ?? (
              <Button variant="quiet" className="text-sm" onClick={report} disabled={busy}>
                Reportar un error
              </Button>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

/**
 * "Explicá por qué": una línea propia antes de ver la explicación. Lo escrito queda en la pantalla
 * para compararlo; no se manda a ningún lado.
 */
function ExplainPrompt({ onDone }: { onDone: (text: string) => void }) {
  const id = useId();
  const [text, setText] = useState("");
  return (
    <form
      className="max-w-xl"
      onSubmit={(e) => {
        e.preventDefault();
        if (text.trim()) onDone(text.trim());
      }}
    >
      <p className="mb-4 font-semibold text-ok">Bien, pero dudaste.</p>
      <label htmlFor={id} className="mb-1.5 block font-medium">
        Explicá por qué: escribilo en una línea
      </label>
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-3">
        <input
          id={id}
          type="text"
          autoComplete="off"
          maxLength={200}
          value={text}
          autoFocus
          onChange={(e) => setText(e.target.value)}
          className={`min-w-0 flex-1 basis-56 text-[1.0625rem] ${FIELD}`}
        />
        <div className="flex items-baseline gap-5">
          <Button type="submit" variant="secondary" disabled={!text.trim()}>
            Listo
          </Button>
          <Button variant="quiet" className="text-sm" onClick={() => onDone("")}>
            Saltear
          </Button>
        </div>
      </div>
      <p className="mt-2 text-sm text-muted">Después ves la explicación y la comparás con la tuya.</p>
    </form>
  );
}

/** Lo que escribió, al lado de la explicación de arriba, y "¿Dijiste lo mismo?". */
function ExplainCompare({ text, answerId }: { text: string; answerId: number }) {
  const id = useId();
  const [answered, setAnswered] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  async function send(same: boolean) {
    setBusy(true);
    setError(false);
    try {
      await api.explain(answerId, same);
      setAnswered(true);
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-6 max-w-xl">
      <Label className="mb-1">Tu explicación</Label>
      <p className="text-[1.0625rem] leading-relaxed">{text}</p>
      {answered ? (
        <p role="status" className="mt-4 text-sm text-muted">
          Anotado. Cuenta para el repaso.
        </p>
      ) : (
        <div role="group" aria-labelledby={id} className="mt-4 flex flex-wrap items-baseline gap-x-4 gap-y-2">
          <p id={id} className="font-medium">
            ¿Dijiste lo mismo?
          </p>
          <div className="flex gap-2">
            <Button variant="secondary" onClick={() => send(true)} disabled={busy}>
              Sí
            </Button>
            <Button variant="secondary" onClick={() => send(false)} disabled={busy}>
              No
            </Button>
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-2 text-sm text-bad">
          No se pudo guardar. Probá de nuevo.
        </p>
      )}
    </div>
  );
}

/** "Repaso mezclado · 10 min" y después "Foco: Límites · 40 min"; sin sesión, solo si hay mezcla. */
function phaseTitle(item: Shown, session?: SessionInfo): string | null {
  if (session) {
    if (item.phase === "review") return `Repaso mezclado · ${session.review_minutes} min`;
    return `Foco: ${session.topic} · ${session.focus_minutes} min`;
  }
  return item.phase === "review" ? "Repaso mezclado" : null;
}

/** La corrección según el resultado y la confianza. */
function FeedbackBody({
  feedback,
  correctText,
  typed,
  source,
}: {
  feedback: Feedback;
  correctText: string;
  /** Respuesta escrita: la línea "Escribiste …" ya dice si es correcta. */
  typed: boolean;
  source: ReactNode;
}) {
  const why = (
    <>
      <h2 className="math mb-2 text-[1.0625rem] font-semibold">
        {typed ? "Respuesta correcta: " : "Por qué es "}
        {keepMath(correctText)}
      </h2>
      <p className="math whitespace-pre-line text-[1.0625rem] leading-relaxed">{keepMath(feedback.explanation)}</p>
    </>
  );
  const commonError = feedback.common_error && (
    <p className="math text-[1.0625rem] leading-relaxed">
      <span className="font-semibold">Error común:</span> {keepMath(feedback.common_error)}
    </p>
  );

  switch (feedback.feedback_style) {
    case "alarm":
      return (
        <>
          <p className="mb-4 font-semibold text-bad">Estabas seguro y era otra. Fijate bien por qué.</p>
          {commonError && <div className="mb-5">{commonError}</div>}
          {why}
          {source}
        </>
      );
    case "doubt":
      return (
        <>
          <p className="mb-4 font-semibold text-ok">Bien, pero dudaste: leé por qué.</p>
          {why}
          {commonError && <div className="mt-4">{commonError}</div>}
          {source}
        </>
      );
    case "short":
      return (
        <>
          <p className="mb-3 font-semibold text-ok">Correcto</p>
          <details className="group">
            <summary className="w-fit cursor-pointer list-none text-sm text-muted underline decoration-line underline-offset-4 hover:text-ink [&::-webkit-details-marker]:hidden">
              <span className="group-open:hidden">Ver por qué</span>
              <span className="hidden group-open:inline">Ocultar</span>
            </summary>
            <div className="mt-4">
              {why}
              {commonError && <div className="mt-4">{commonError}</div>}
              {source}
            </div>
          </details>
        </>
      );
    default:
      return (
        <>
          {!typed && (
            <p className={`mb-4 font-semibold ${feedback.correct ? "text-ok" : "text-bad"}`}>
              {feedback.correct ? "Correcta, aunque adivinaste" : "No es correcta"}
            </p>
          )}
          {why}
          {commonError && <div className="mt-4">{commonError}</div>}
          {source}
        </>
      );
  }
}

export type OptionState = "idle" | "picked" | "right" | "rightPicked" | "wrongPicked" | "faded";

export function optionState(original: number, chosen: number | null, feedback: Feedback | null): OptionState {
  if (!feedback) return chosen === original ? "picked" : "idle";
  if (original === feedback.correct_index) return original === chosen ? "rightPicked" : "right";
  if (original === chosen) return "wrongPicked";
  return "faded";
}

const OPTION_STYLES: Record<OptionState, string> = {
  idle: "border-line hover:bg-hover cursor-pointer",
  picked: "border-ink cursor-pointer",
  right: "border-ok",
  rightPicked: "border-ok",
  wrongPicked: "border-bad",
  faded: "border-line text-muted",
};

const OPTION_TAGS: Partial<Record<OptionState, [string, string]>> = {
  right: ["Correcta", "text-ok"],
  rightPicked: ["Tu respuesta, correcta", "text-ok"],
  wrongPicked: ["Tu respuesta, incorrecta", "text-bad"],
};

/**
 * Sin juicio (preguntas para arrancar): la correcta se marca en tinta y lo elegido solo dice
 * "Tu respuesta". Nada en verde ni en rojo, porque no es una evaluación.
 */
const NEUTRAL_STYLES: Partial<Record<OptionState, string>> = {
  right: "border-ink",
  rightPicked: "border-ink",
  wrongPicked: "border-line",
};

const NEUTRAL_TAGS: Partial<Record<OptionState, [string, string]>> = {
  right: ["Correcta", "text-ink"],
  rightPicked: ["Correcta · tu respuesta", "text-ink"],
  wrongPicked: ["Tu respuesta", "text-muted"],
};

export function OptionButton(props: {
  text: string;
  number: number;
  state: OptionState;
  disabled: boolean;
  onClick: () => void;
  /** Sin verde ni rojo: para las preguntas para arrancar. */
  neutral?: boolean;
}) {
  const tag = (props.neutral ? NEUTRAL_TAGS : OPTION_TAGS)[props.state];
  const style = (props.neutral && NEUTRAL_STYLES[props.state]) || OPTION_STYLES[props.state];
  return (
    <button
      type="button"
      onClick={props.onClick}
      disabled={props.disabled}
      aria-pressed={props.state === "picked" ? true : props.state === "idle" ? false : undefined}
      className={`press flex w-full items-baseline gap-4 rounded-lg border px-4 py-3 text-left text-[1.0625rem] ${style}`}
    >
      <span className="math min-w-0 flex-1">{props.text}</span>
      {tag ? (
        <span className={`shrink-0 text-sm font-medium ${tag[1]}`}>{tag[0]}</span>
      ) : (
        props.state !== "faded" && (
          // Atajo de teclado: no tiene sentido en pantallas táctiles ni después de responder.
          <kbd className="shrink-0 font-sans text-xs text-faint [@media(pointer:coarse)]:hidden" aria-hidden>
            {props.number}
          </kbd>
        )
      )}
    </button>
  );
}

function sourceUrl(youtubeId: string, seconds: number | null): string {
  const base = `https://youtu.be/${youtubeId}`;
  return seconds == null ? base : `${base}?t=${seconds}`;
}

function sourceText(title: string | undefined, seconds: number | null): string {
  const name = title ?? "Ver la clase";
  return seconds == null ? name : `${name}, minuto ${formatTimestamp(seconds)}`;
}
