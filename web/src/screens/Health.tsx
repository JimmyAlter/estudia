// "¿Llegás?": una línea por examen, con por qué y qué hacer hoy.
import type { ExamHealth, PlanSession, QuizParams, SessionInfo } from "../api";
import { aboutTime, freeTime, tidy } from "../format";

const VERDICT: Record<ExamHealth["verdict"], string> = {
  llegás: "llegás",
  justo: "llegás justo",
  "no llegás": "no llegás",
};

/** "A", "A y B", "A, B y C". */
export function listNames(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} y ${names[names.length - 1]}`;
}

/** Qué se hace en una sesión, en minutos: "40 min de Límites", "10 min de repaso mezclado y 40 de Límites". */
export function sessionText(session: SessionInfo & { minutes?: number }): string {
  if (session.kind === "mixed_only") return `${session.review_minutes || session.minutes} min de repaso mezclado`;
  if (session.kind === "mixed_focus" && session.review_minutes) {
    return `${session.review_minutes} min de repaso mezclado y ${session.focus_minutes} de ${session.topic}`;
  }
  return `${session.focus_minutes || session.minutes} min de ${session.topic}`;
}

export function sessionInfo(session: PlanSession): SessionInfo {
  return {
    kind: session.kind,
    topic: session.topic,
    review_minutes: session.review_minutes,
    focus_minutes: session.focus_minutes,
  };
}

export function HealthLine({
  health,
  onPractice,
  withExamName = true,
}: {
  health: ExamHealth;
  onPractice: (params: QuizParams, session?: SessionInfo) => void;
  /** Sin el nombre cuando el examen ya es el título de la pantalla. */
  withExamName?: boolean;
}) {
  const why =
    health.verdict !== "llegás" && health.weak_topics.length
      ? `Te faltan ${listNames(health.weak_topics.map((t) => t.topic))}, ${aboutTime(health.minutes_needed)}, y tenés ${freeTime(health.minutes_available)} libres.`
      : null;
  const today = health.today;
  return (
    <p className="text-[0.9375rem] leading-relaxed">
      <span className="font-medium">
        {withExamName
          ? `${tidy(health.exam)}: con tu tiempo actual ${VERDICT[health.verdict]}.`
          : `Con tu tiempo actual ${VERDICT[health.verdict]}.`}
      </span>{" "}
      {why && <span className="text-muted">{why} </span>}
      {today && (
        <button
          type="button"
          onClick={() => onPractice({ session_id: today.id }, sessionInfo(today))}
          className="press cursor-pointer underline decoration-line underline-offset-4 hover:decoration-ink"
        >
          Hoy: {sessionText(today)}
        </button>
      )}
    </p>
  );
}
