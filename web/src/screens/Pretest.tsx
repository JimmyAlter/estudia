import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Feedback, type PretestOffer, type QuizItem } from "../api";
import { keepMath, tidy } from "../format";
import { Button, DocumentSource, Label } from "../ui";
import type { Screen } from "./Home";
import { OptionButton, optionState, shuffled } from "./Practice";

const NORMAL = "Es normal no saberlas todavía";

/** Pregunta con sus opciones en el orden en que se muestran (`order[i]` = índice original). */
interface Shown extends QuizItem {
  order: number[];
}

/**
 * "3 preguntas para arrancar": antes de mirar una clase nueva. No se cuenta nada: después de cada
 * una se ve la correcta y por qué, y al final se invita a mirar la clase.
 */
export default function Pretest({ onNavigate }: { onNavigate: (screen: Screen) => void }) {
  const [offer, setOffer] = useState<PretestOffer | null>(null);
  const [items, setItems] = useState<Shown[] | null>(null);
  const [index, setIndex] = useState(0);
  const [chosen, setChosen] = useState<number | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const feedbackRef = useRef<HTMLElement>(null);
  const stemRef = useRef<HTMLHeadingElement>(null);
  const closed = useRef(false);

  useEffect(() => {
    let alive = true;
    api
      .home()
      .then(async (home) => {
        if (!alive) return;
        if (!home.pretest) {
          setUnavailable(true);
          return;
        }
        const quiz = await api.startPretest(home.pretest.material_id);
        if (!alive) return;
        setOffer(home.pretest);
        setItems(quiz.map((q) => ({ ...q, order: shuffled(q.options.length) })));
      })
      .catch(() => alive && setUnavailable(true));
    return () => {
      alive = false;
    };
  }, []);

  const item = items?.[index];
  const finished = items !== null && index >= items.length;

  // Terminado: no se vuelve a ofrecer esa clase.
  useEffect(() => {
    if (!finished || !offer || closed.current) return;
    closed.current = true;
    api.closePretest(offer.material_id).catch(() => {});
  }, [finished, offer]);

  useEffect(() => {
    if (feedback) feedbackRef.current?.focus();
  }, [feedback]);

  const answer = useCallback(
    async (original: number) => {
      if (!item || feedback || busy) return;
      setChosen(original);
      setBusy(true);
      setError(null);
      try {
        setFeedback(
          await api.pretestAnswer({
            question_id: item.question_id,
            chosen_index: item.order.indexOf(original),
            shown_order: item.order,
          }),
        );
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
    setFeedback(null);
    setChosen(null);
    setError(null);
    setIndex((i) => i + 1);
    window.setTimeout(() => stemRef.current?.focus({ preventScroll: true }), 0);
  }, []);

  // Teclado: 1 a 4 en el orden en que se ven las opciones, Enter para seguir.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (!item || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (!feedback) {
        const n = Number(event.key);
        if (n >= 1 && n <= item.order.length) {
          event.preventDefault();
          answer(item.order[n - 1]);
        }
      } else if (event.key === "Enter" && !target?.closest("button, a")) {
        event.preventDefault();
        next();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [item, feedback, answer, next]);

  if (unavailable) {
    return (
      <div className="max-w-xl">
        <h1 className="mb-3 font-serif text-[2rem] leading-tight tracking-[-0.02em]">3 preguntas para arrancar</h1>
        <p className="mb-8 text-muted">Ahora no hay una clase nueva para arrancar.</p>
        <Button variant="secondary" onClick={() => onNavigate("inicio")}>
          Volver al inicio
        </Button>
      </div>
    );
  }
  if (!items || !offer) return null;
  // Un PDF o unas diapositivas se leen; un video se mira.
  const isDocument = offer.kind === "pdf" || offer.kind === "slides";

  if (finished) {
    return (
      <div className="reveal max-w-xl">
        <Label className="mb-2">{tidy(offer.title)}</Label>
        <h1 className="mb-8 font-serif text-[2rem] leading-tight tracking-[-0.02em]">
          {isDocument ? "Listo. Ahora leé el apunte: te va a rendir más." : "Listo. Ahora mirá la clase: te va a rendir más."}
        </h1>
        <div className="flex flex-wrap gap-3">
          <Button onClick={() => onNavigate("materiales")}>{isDocument ? "Ver el apunte" : "Ver la clase"}</Button>
          <Button variant="secondary" onClick={() => onNavigate("inicio")}>
            Volver al inicio
          </Button>
        </div>
      </div>
    );
  }

  const current = item!;
  const last = index + 1 >= items.length;
  return (
    <div className="max-w-2xl">
      <Label className="mb-1">{tidy(offer.title)}</Label>
      <p className="mb-5 text-sm text-muted">{NORMAL}</p>
      <h1 ref={stemRef} tabIndex={-1} className="math mb-7 text-[1.45rem] leading-snug tracking-[-0.01em]">
        {keepMath(current.stem)}
      </h1>

      <ol className="space-y-2">
        {current.order.map((original, shownAt) => (
          <li key={original}>
            <OptionButton
              text={keepMath(current.options[original])}
              number={shownAt + 1}
              state={optionState(original, chosen, feedback)}
              disabled={feedback !== null || busy}
              onClick={() => answer(original)}
              neutral
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
        <section ref={feedbackRef} tabIndex={-1} className="reveal mt-8 border-t border-line pt-6 outline-none">
          {/* La correcta y la explicación tal como las manda el servidor. */}
          <h2 className="math mb-2 text-[1.0625rem] font-semibold">
            Por qué es {keepMath(current.options[feedback.correct_index])}
          </h2>
          <p className="math whitespace-pre-line text-[1.0625rem] leading-relaxed">{keepMath(feedback.explanation)}</p>
          {feedback.common_error && (
            <p className="math mt-4 text-[1.0625rem] leading-relaxed">
              <span className="font-semibold">Error común:</span> {keepMath(feedback.common_error)}
            </p>
          )}
          {feedback.source_label && (
            <DocumentSource className="mt-4" label={feedback.source_label} url={feedback.source_url} />
          )}
          <div className="mt-8">
            <Button onClick={next}>{last ? "Terminar" : "Siguiente"}</Button>
          </div>
        </section>
      )}
    </div>
  );
}
