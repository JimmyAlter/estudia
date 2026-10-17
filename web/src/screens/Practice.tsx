import { useCallback, useEffect, useState } from "react";
import { api, type Feedback, type QuizItem, type QuizParams } from "../api";
import { formatTimestamp, keepMath, plural, tidy } from "../format";
import { Button, Label, Notice } from "../ui";

const UNDO_MS = 5000;

interface Props {
  params: QuizParams;
  onExit: () => void;
}

/** Pregunta con sus opciones en el orden en que se muestran (`order[i]` = índice original). */
interface Shown extends QuizItem {
  order: number[];
}

interface Result {
  item: Shown;
  correct: boolean;
}

function shuffled(length: number): number[] {
  const order = Array.from({ length }, (_, i) => i);
  for (let i = order.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [order[i], order[j]] = [order[j], order[i]];
  }
  return order;
}

/** Cada intento mezcla las opciones de nuevo, también al repetir lo fallado. */
function withShuffledOptions(quiz: QuizItem[]): Shown[] {
  return quiz.map((q) => ({ ...q, order: shuffled(q.options.length) }));
}

export default function Practice({ params, onExit }: Props) {
  const [items, setItems] = useState<Shown[] | null>(null);
  const [index, setIndex] = useState(0);
  /** Índice original de la opción elegida. */
  const [chosen, setChosen] = useState<number | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [results, setResults] = useState<Result[]>([]);
  const [reported, setReported] = useState<number[]>([]);
  const [undo, setUndo] = useState<Shown | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [classTitles, setClassTitles] = useState<Map<string, string>>(new Map());

  const start = useCallback((quiz: QuizItem[]) => {
    setItems(withShuffledOptions(quiz));
    setIndex(0);
    setChosen(null);
    setFeedback(null);
    setResults([]);
    setReported([]);
    setUndo(null);
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

  // El aviso de "Deshacer" dura unos segundos.
  useEffect(() => {
    if (!undo) return;
    const timer = window.setTimeout(() => setUndo(null), UNDO_MS);
    return () => window.clearTimeout(timer);
  }, [undo]);

  const item = items?.[index];
  const finished = items !== null && index >= items.length;

  const choose = useCallback(
    async (original: number) => {
      if (!item || feedback || busy) return;
      setBusy(true);
      setChosen(original);
      setError(null);
      try {
        const fb = await api.answer(item.question_id, original);
        setFeedback(fb);
        setResults((r) => [...r, { item, correct: fb.correct }]);
      } catch {
        setChosen(null);
        setError("No se pudo guardar la respuesta. Probá de nuevo.");
      } finally {
        setBusy(false);
      }
    },
    [item, feedback, busy],
  );

  const next = useCallback(() => {
    setChosen(null);
    setFeedback(null);
    setError(null);
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
      setChosen(null);
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

  // Teclado: 1–4 en el orden en que se ven las opciones, Enter para seguir.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (!item || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.target instanceof HTMLElement && event.target.closest("input, textarea, select")) return;
      const n = Number(event.key);
      if (!feedback && n >= 1 && n <= item.order.length) {
        event.preventDefault();
        choose(item.order[n - 1]);
      } else if (feedback && event.key === "Enter" && !(event.target instanceof HTMLButtonElement)) {
        event.preventDefault();
        next();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [item, feedback, choose, next]);

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
            ? "Lo que fallaste vuelve en las próximas prácticas hasta que te salga dos veces seguidas."
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

  return (
    <div className="max-w-2xl">
      <div className="mb-1 flex min-h-7 flex-wrap items-baseline justify-between gap-x-6 gap-y-1 sm:mb-4">
        <Label className="whitespace-nowrap">
          <span className="max-sm:hidden">{current.topic} · </span>
          {number}
        </Label>
        {undoLine ?? (
          <Button variant="quiet" className="shrink-0 text-sm" onClick={report} disabled={busy}>
            Reportar error en la pregunta
          </Button>
        )}
      </div>
      <p className="mb-4 text-sm text-muted sm:hidden">{current.topic}</p>

      <h1 className="math mb-7 text-[1.45rem] leading-snug tracking-[-0.01em]">{keepMath(current.stem)}</h1>

      <ol className="space-y-2">
        {current.order.map((original, shownAt) => (
          <li key={original}>
            <OptionButton
              text={keepMath(current.options[original])}
              number={shownAt + 1}
              state={optionState(original, chosen, feedback)}
              disabled={feedback !== null || busy}
              onClick={() => choose(original)}
            />
          </li>
        ))}
      </ol>

      {error && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error}
        </p>
      )}

      {feedback && (
        <section className="reveal mt-8 border-t border-line pt-6">
          <h2 className="math mb-2 text-[1.0625rem] font-semibold">Por qué es {keepMath(current.options[feedback.correct_index])}</h2>
          <p className="math whitespace-pre-line text-[1.0625rem] leading-relaxed">{keepMath(feedback.explanation)}</p>
          {feedback.common_error && (
            <p className="math mt-4 text-[1.0625rem] leading-relaxed">
              <span className="font-semibold">Error común:</span> {keepMath(feedback.common_error)}
            </p>
          )}
          {feedback.youtube_id && (
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
          )}
          <div className="mt-8">
            <Button onClick={next} autoFocus>
              {index + 1 < items.length ? "Siguiente" : "Ver resultado"}
            </Button>
          </div>
        </section>
      )}
    </div>
  );
}

type OptionState = "idle" | "picked" | "right" | "rightPicked" | "wrongPicked" | "faded";

function optionState(original: number, chosen: number | null, feedback: Feedback | null): OptionState {
  if (!feedback) return chosen === original ? "picked" : "idle";
  if (original === feedback.correct_index) return original === chosen ? "rightPicked" : "right";
  if (original === chosen) return "wrongPicked";
  return "faded";
}

const OPTION_STYLES: Record<OptionState, string> = {
  idle: "border-line hover:bg-hover cursor-pointer",
  picked: "border-ink",
  right: "border-ok",
  rightPicked: "border-ok",
  wrongPicked: "border-bad",
  faded: "border-line text-muted",
};

const OPTION_TAGS: Partial<Record<OptionState, [string, string]>> = {
  right: ["Correcta", "text-ok"],
  rightPicked: ["Tu respuesta, correcta", "text-ok"],
  wrongPicked: ["Tu respuesta", "text-bad"],
};

function OptionButton(props: {
  text: string;
  number: number;
  state: OptionState;
  disabled: boolean;
  onClick: () => void;
}) {
  const tag = OPTION_TAGS[props.state];
  return (
    <button
      type="button"
      onClick={props.onClick}
      disabled={props.disabled}
      className={`press flex w-full items-baseline gap-4 rounded-lg border px-4 py-3 text-left text-[1.0625rem] ${OPTION_STYLES[props.state]}`}
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
