// Cliente de la API local (mismo origen que la página).

export type Step = "facultad" | "login" | "edad" | "ia" | "listo";
export type Age = "adult" | "minor";

export interface AppState {
  step: Step;
  /** La respuesta a "¿Tenés 18 años o más?"; null: todavía no contestó. */
  age?: Age | null;
  site_id: string | null;
  ai_stop_reason: "quota" | "youtube_cap" | "unavailable" | "auth" | null;
  // Con cuota agotada: cuándo vuelve el primer modelo de Gemini (epoch en segundos).
  ai_quota_resets_at?: number | null;
  syncing: boolean;
  last_sync_at: number | null;
  last_sync_error: string | null;
}

export interface VersionInfo {
  current: string;
  latest: { version: string; url: string } | null;
}

export interface Site {
  id: string;
  name: string;
  university: string;
}

export interface Exam {
  name: string;
  course: string;
  opens_at: number;
  days_left: number;
  /** Los exámenes cargados a mano no traen intentos ni límite. */
  attempts: number | null;
  time_limit_s: number | null;
  max_grade: number;
}

export interface Topic {
  id: number;
  name: string;
  /** Probabilidad de saberlo el día de su examen más próximo (o hoy, si no tiene). */
  mastery: number;
  practiced: boolean;
  /** Preguntas (verificadas, no retiradas) ya vistas, y cuántas hay. */
  seen: number;
  total: number;
  /** Fecha del examen al que se refiere `mastery`; null: hoy. */
  at: number | null;
}

export interface HomeData {
  exam: Exam | null;
  topics: Topic[];
  suggestion: Topic | null;
  processing: { pending: number; done: number; failed: number };
}

export type MaterialStatus = "pending" | "done" | "failed" | "unsupported" | "needs_public_link";

export interface Material {
  id: number;
  title: string;
  kind: string;
  status: MaterialStatus;
  summary_md: string | null;
  error: string | null;
  youtube_id: string | null;
  /** Página de la clase en el campus (para lo que Estudia no puede preparar). */
  campus_url: string | null;
  /** Temas de la clase y cuántas preguntas se pueden practicar de ellos. */
  topic_ids: number[];
  questions: number;
}

export interface Section {
  id: number;
  course_id: number;
  course: string | null;
  num: number;
  name: string;
  materials: Material[];
}

/** Cómo se muestra el ítem: opciones, "pensalo primero" (opciones ocultas) o respuesta escrita. */
export type QuizFormat = "mcq" | "think_first" | "typed";
/** Dentro de una sesión del plan: repaso mezclado de vencidas primero, después el tema foco. */
export type QuizPhase = "review" | "focus";

export interface QuizItem {
  question_id: number;
  stem: string;
  options: string[];
  topic: string;
  format: QuizFormat;
  phase: QuizPhase;
  /** En reaprendizaje: qué día sería acertarla ahora (de 3). */
  relearning: { day: 1 | 2 | 3 } | null;
}

/**
 * Feedback según resultado y confianza:
 * alarm = error con "Seguro"; full = error dudando; short = acierto con "Seguro";
 * doubt = acierto con "Me parece" o "Adiviné".
 */
export type FeedbackStyle = "alarm" | "full" | "short" | "doubt";

export interface Feedback {
  correct: boolean;
  /** Índices en el orden original de `options`, no en el mostrado. */
  correct_index: number;
  chosen_index: number | null;
  explanation: string;
  common_error: string | null;
  source_seconds: number | null;
  youtube_id: string | null;
  /** Solo en respuestas escritas. */
  typed_ok: boolean | null;
  /** Vuelve al final de la sesión. */
  requeue: boolean;
  /** "Vuelve mañana", "Vuelve en 2 días", "La sabés: vuelve en 9 días". */
  next_label: string | null;
  /** "1 de 3 días" mientras está en reaprendizaje. */
  relearn_progress: string | null;
  feedback_style: FeedbackStyle;
  /** De qué pregunta es (la revisión del simulacro se arma por id). */
  question_id?: number | null;
}

export interface QuizParams {
  topic_id?: number;
  /** Varios temas mezclados. */
  topic_ids?: number[];
  /** Sesión del plan: arma la mezcla de repaso y foco. */
  session_id?: number;
  weakest?: boolean;
  /** De 1 a 50; por defecto 10. */
  size?: number;
}

/** 0 = Adiviné, 1 = Me parece, 2 = Seguro. */
export type Confidence = 0 | 1 | 2;
export type AnswerMode = "mcq" | "mcq_think_first" | "typed" | "typed_then_mcq";

/**
 * Cuerpo de POST /api/answer. Un `typed_text` ilegible vuelve con 400 ("No pude leer la
 * expresión") y no se registra: se pide reescribirlo. Si no se pudo corregir en ese momento
 * vuelve 503 ("No pude corregir ahora, probá de nuevo"), tampoco registrado.
 */
export interface AnswerBody {
  question_id: number;
  /** Posición tocada dentro de `shown_order`; null en una respuesta escrita. */
  chosen_index: number | null;
  /** Hasta 200 caracteres. */
  typed_text: string | null;
  confidence: Confidence;
  mode: AnswerMode;
  latency_ms: number;
  /** "Pensalo primero": ms desde el enunciado hasta abrir las opciones. */
  options_revealed_ms?: number | null;
  /** Índices originales en el orden en que se mostraron; vacío en una respuesta escrita. */
  shown_order: number[];
}

export type SessionKind = "focus" | "mixed_focus" | "mixed_only";

/** Sesión del plan (en la agenda y en el "¿Llegás?"). */
export interface PlanSession {
  id: number;
  course: string;
  topic_id: number;
  topic: string;
  exam_id: number | null;
  starts_at: number;
  minutes: number;
  done: boolean;
  kind: SessionKind;
  topic_ids: number[];
  review_minutes: number;
  focus_minutes: number;
  /** "Foco: Límites", "Repaso mezclado: Límites y Derivadas". */
  label: string;
}

/** Lo que la práctica necesita saber de una sesión del plan para el encabezado de cada fase. */
export type SessionInfo = Pick<PlanSession, "kind" | "topic" | "review_minutes" | "focus_minutes">;

export interface WeakTopic {
  topic_id: number;
  topic: string;
  probability: number;
  minutes: number;
}

export type Verdict = "llegás" | "justo" | "no llegás";

export interface ExamHealth {
  exam_id: number;
  exam: string;
  course: string;
  at: number;
  minutes_needed: number;
  minutes_available: number;
  verdict: Verdict;
  due_reviews: number;
  /** Por qué: los temas a los que más les falta. */
  weak_topics: WeakTopic[];
  /** Qué hacer: la próxima sesión de hoy para ese examen. */
  today: PlanSession | null;
}

export type AgendaKind = "class" | "exam" | "deadline" | "event" | "session";

export interface AgendaItem {
  kind: AgendaKind;
  id: number;
  title: string;
  course: string | null;
  starts_at: number;
  ends_at?: number;
  place?: string | null;
  url?: string | null;
  // Solo las sesiones de estudio:
  topic_id?: number;
  topic?: string;
  done?: boolean;
  session_kind?: SessionKind;
  topic_ids?: number[];
  exam_id?: number | null;
  review_minutes?: number;
  focus_minutes?: number;
}

export interface AgendaWeek {
  /** Los 7 días desde el lunes, "2026-10-12". */
  days: { date: string; items: AgendaItem[] }[];
  health: ExamHealth[];
}

export interface Course {
  id: number;
  name: string;
}

export interface ExamResult {
  grade: number;
  max_grade: number;
  at: number;
}

export interface ExamInfo {
  id: number;
  course_id: number;
  course: string;
  name: string;
  at: number | null;
  closes_at: number | null;
  source: string;
  topic_ids: number[];
  max_grade: number | null;
  time_limit_s: number | null;
  attempts: number | null;
  editable: boolean;
  result: ExamResult | null;
  /** "Prefiero no decir": no se vuelve a preguntar la nota. */
  result_skipped: boolean;
}

export interface Slot {
  id: number;
  course_id: number;
  course: string;
  /** 0 = lunes. */
  weekday: number;
  start_min: number;
  end_min: number;
  place: string | null;
}

export interface SlotSuggestion {
  course_id: number;
  course_name: string;
  start_min: number;
  end_min: number;
}

export interface SlotBody {
  course_id: number;
  weekday: number;
  start_min: number;
  end_min: number;
  place: string | null;
}

export interface PlanSettings {
  /** Minutos por día, de lunes (0) a domingo (6). */
  availability: number[];
  study_start_min: number;
}

export interface MockStart {
  attempt_id: number;
  items: QuizItem[];
  seconds: number;
  max_grade: number;
  predicted_grade: number | null;
}

export type MockAnswer = { index: number } | { typed: string };

export interface MockResult {
  grade: number;
  max_grade: number;
  correct: number;
  total: number;
  failed_topics: string[];
  /** Una por pregunta, en el orden del intento. */
  review: Feedback[];
  late: boolean;
  predicted_grade: number | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

async function request<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const options: RequestInit = { method: init?.method ?? "GET" };
  if (init?.body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, (data && data.detail) || response.statusText);
  }
  return data as T;
}

const post = <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body });
const put = <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body });
const del = <T>(path: string) => request<T>(path, { method: "DELETE" });

export const api = {
  /** Descarga directa del .apkg; sin curso, uno con todos. */
  ankiUrl: (courseId?: number) => `/api/export/anki.apkg${courseId == null ? "" : `?course_id=${courseId}`}`,
  state: () => request<AppState>("/api/state"),
  version: () => request<VersionInfo>("/api/version"),
  catalog: () => request<Site[]>("/api/catalog"),
  chooseSite: (siteId: string) => post<{ ok: true }>("/api/site", { site_id: siteId }),
  startLogin: () => post<{ url: string }>("/api/login/start"),
  /** Enlace de vuelta del campus pegado a mano (lleva el token: no se muestra ni se registra). */
  loginCallback: (url: string) => post<{ ok: true }>("/api/login/callback", { url }),
  setAiKey: (key: string) => post<{ ok: true }>("/api/ai", { provider: "gemini", key }),
  sync: () => post<{ started: boolean }>("/api/sync"),
  home: () => request<HomeData>("/api/home"),
  materials: () => request<Section[]>("/api/materials"),
  retryFailed: () => post<{ reset: number }>("/api/materials/retry-failed"),
  quiz: (params: QuizParams) => post<QuizItem[]>("/api/quiz", params),
  answer: (body: AnswerBody) => post<Feedback>("/api/answer", body),
  retire: (questionId: number) => post<{ ok: true }>(`/api/question/${questionId}/retire`),
  unretire: (questionId: number) => post<{ ok: true }>(`/api/question/${questionId}/unretire`),
  /** Sin semana, la actual. */
  agenda: (weekStart?: number) => request<AgendaWeek>(weekStart == null ? "/api/agenda" : `/api/agenda?week_start=${weekStart}`),
  calendarUrl: "/api/calendar.ics",
  health: () => request<ExamHealth[]>("/api/plan/health"),
  courses: () => request<Course[]>("/api/courses"),
  exams: () => request<ExamInfo[]>("/api/exams"),
  createExam: (body: { course_id: number; name: string; at: number; max_grade: number | null }) =>
    post<{ id: number }>("/api/exams", body),
  examTopics: (examId: number) => request<{ id: number; name: string }[]>(`/api/exams/${examId}/topics`),
  examResult: (examId: number, grade: number) => post<ExamResult>(`/api/exams/${examId}/result`, { grade }),
  skipExamResult: (examId: number) => post<{ ok: true }>(`/api/exams/${examId}/result/skip`),
  schedule: () => request<{ slots: Slot[]; suggestions: SlotSuggestion[] }>("/api/schedule"),
  addSlot: (body: SlotBody) => post<{ id: number }>("/api/schedule", body),
  editSlot: (id: number, body: SlotBody) => put<{ ok: true }>(`/api/schedule/${id}`, body),
  deleteSlot: (id: number) => del<{ ok: true }>(`/api/schedule/${id}`),
  dismissSuggestion: (courseId: number) => post<{ ok: true }>(`/api/schedule/suggestions/${courseId}/dismiss`),
  planSettings: () => request<PlanSettings>("/api/plan/settings"),
  savePlanSettings: (body: PlanSettings) => put<{ ok: true }>("/api/plan/settings", body),
  settings: () => request<{ reminders: boolean; age: Age | null; allow_download?: boolean }>("/api/settings"),
  saveSettings: (body: { reminders?: boolean; age?: Age; allow_download?: boolean }) => put<{ ok: true }>("/api/settings", body),
  startMock: (examId: number, predictedGrade: number | null) =>
    post<MockStart>("/api/mock/start", { exam_id: examId, predicted_grade: predictedGrade }),
  finishMock: (attemptId: number, answers: Record<number, MockAnswer>) =>
    post<MockResult>(`/api/mock/${attemptId}/finish`, { answers }),
  /** El resultado de un simulacro ya entregado. */
  mockResult: (attemptId: number) => request<MockResult>(`/api/mock/${attemptId}`),
};
