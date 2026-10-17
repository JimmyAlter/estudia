// Cliente de la API local (mismo origen que la página).

export type Step = "facultad" | "login" | "ia" | "listo";

export interface AppState {
  step: Step;
  site_id: string | null;
  ai_stop_reason: "quota" | "unavailable" | "auth" | null;
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
  attempts: number;
  time_limit_s: number;
  max_grade: number;
}

export interface Topic {
  id: number;
  name: string;
  mastery: number;
  practiced: boolean;
}

export interface HomeData {
  exam: Exam | null;
  topics: Topic[];
  suggestion: Topic | null;
  processing: { pending: number; done: number; failed: number };
}

export type MaterialStatus = "pending" | "done" | "failed" | "unsupported";

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
}

export interface Section {
  id: number;
  course_id: number;
  course: string | null;
  num: number;
  name: string;
  materials: Material[];
}

export interface QuizItem {
  question_id: number;
  stem: string;
  options: string[];
  topic: string;
}

export interface Feedback {
  correct: boolean;
  correct_index: number;
  explanation: string;
  common_error: string | null;
  source_seconds: number | null;
  youtube_id: string | null;
}

export interface QuizParams {
  topic_id?: number;
  weakest?: boolean;
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

export const api = {
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
  answer: (questionId: number, chosenIndex: number) =>
    post<Feedback>("/api/answer", { question_id: questionId, chosen_index: chosenIndex }),
  retire: (questionId: number) => post<{ ok: true }>(`/api/question/${questionId}/retire`),
  unretire: (questionId: number) => post<{ ok: true }>(`/api/question/${questionId}/unretire`),
};
