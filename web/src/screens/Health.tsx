// "¿Llegás?": una línea por examen, con por qué y qué hacer hoy.
import type { ExamHealth, PlanSession, QuizParams, SessionInfo } from "../api";
import { aboutTime, dayKey, freeTime, tidy, weekdayAndDay } from "../format";

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

/** Día local de un instante, dicho corto: "hoy", "mañana" o "martes 20". */
export function relativeDay(epochSeconds: number, now = Date.now() / 1000): string {
  const key = dayKey(epochSeconds);
  if (key === dayKey(now)) return "hoy";
  // Mediodía local de mañana: sin saltos de hora en Buenos Aires.
  const tomorrow = dayKey(Date.parse(`${dayKey(now)}T12:00:00-03:00`) / 1000 + 86400);
  if (key === tomorrow) return "mañana";
  return weekdayAndDay(key);
}

/** "Hoy", "Mañana", "Martes 20": el día del simulacro corto como rótulo de botón. */
function dayLabel(epochSeconds: number): string {
  const day = relativeDay(epochSeconds);
  return day.charAt(0).toUpperCase() + day.slice(1);
}

/** "para hoy", "para mañana", "para el martes 20". */
function forDay(epochSeconds: number): string {
  const day = relativeDay(epochSeconds);
  return day === "hoy" || day === "mañana" ? `para ${day}` : `para el ${day}`;
}

/** Una línea de este examen tiene algo para hacer (sesión de hoy, simulacro corto) o una aclaración. */
export function hasActionOrNote(health: ExamHealth): boolean {
  return Boolean(health.today || health.note || health.mock_suggested_at != null);
}

const LINK = "press cursor-pointer underline decoration-line underline-offset-4 hover:decoration-ink";

function ShortMock({ health, onShortMock }: { health: ExamHealth; onShortMock?: (examId: number) => void }) {
  const mockAt = health.mock_suggested_at;
  if (mockAt == null) return null;
  // Con botón, solo el botón: el día ya va en él.
  if (onShortMock) {
    return (
      <button type="button" onClick={() => onShortMock(health.exam_id)} className={`${LINK} text-ink`}>
        {dayLabel(mockAt)}: simulacro corto
      </button>
    );
  }
  return <span>Simulacro corto sugerido {forDay(mockAt)}.</span>;
}

function TodayButton({
  health,
  onPractice,
}: {
  health: ExamHealth;
  onPractice: (params: QuizParams, session?: SessionInfo) => void;
}) {
  const today = health.today;
  if (!today) return null;
  return (
    <button type="button" onClick={() => onPractice({ session_id: today.id }, sessionInfo(today))} className={LINK}>
      Hoy: {sessionText(today)}
    </button>
  );
}

export function HealthLine({
  health,
  onPractice,
  onShortMock,
  withExamName = true,
  compact = false,
}: {
  health: ExamHealth;
  onPractice: (params: QuizParams, session?: SessionInfo) => void;
  /** Botón del simulacro corto en un recuperatorio; sin esto, solo se dice el día sugerido. */
  onShortMock?: (examId: number) => void;
  /** Sin el nombre cuando el examen ya es el título de la pantalla. */
  withExamName?: boolean;
  /**
   * Cuando ya se dijo "llegás a todo": sin el veredicto, solo lo que hay para hacer
   * y la aclaración ("Final de Matemática: faltan menos de 4 semanas, el plan…").
   */
  compact?: boolean;
}) {
  if (compact) {
    const note = health.note?.replace(": ", ", ") ?? null;
    const actions = [
      health.today && <TodayButton key="today" health={health} onPractice={onPractice} />,
      health.mock_suggested_at != null && <ShortMock key="mock" health={health} onShortMock={onShortMock} />,
    ].filter(Boolean);
    return (
      <p className="text-[0.9375rem] leading-relaxed">
        {withExamName && (
          <span className="font-medium">
            {tidy(health.exam)}
            {note ? ": " : " · "}
          </span>
        )}
        {note && (
          <span className="text-muted">
            {withExamName ? note.charAt(0).toLowerCase() + note.slice(1) : note}
            {actions.length ? " " : ""}
          </span>
        )}
        {actions.map((action, i) => (
          <span key={i}>
            {i > 0 && " · "}
            {action}
          </span>
        ))}
      </p>
    );
  }

  const why =
    health.verdict !== "llegás" && health.weak_topics.length
      ? `Te faltan ${listNames(health.weak_topics.map((t) => t.topic))}, ${aboutTime(health.minutes_needed)}, y tenés ${freeTime(health.minutes_available)} libres.`
      : null;
  return (
    <div>
      <p className="text-[0.9375rem] leading-relaxed">
        <span className="font-medium">
          {withExamName
            ? `${tidy(health.exam)}: con tu tiempo actual ${VERDICT[health.verdict]}.`
            : `Con tu tiempo actual ${VERDICT[health.verdict]}.`}
        </span>{" "}
        {why && <span className="text-muted">{why} </span>}
        <TodayButton health={health} onPractice={onPractice} />
      </p>
      {/* Aclaración (un final con poco tiempo): una línea tranquila, sin color de alarma. */}
      {health.note && <p className="text-sm leading-relaxed text-muted">{health.note}</p>}
      {health.mock_suggested_at != null && (
        <p className="text-sm leading-relaxed text-muted">
          <ShortMock health={health} onShortMock={onShortMock} />
        </p>
      )}
    </div>
  );
}
