import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Home from "./Home";
import type { ExamHealth, ExamInfo, HomeData } from "../api";
import { mockApi } from "../test-utils";

const NOW = Math.floor(Date.now() / 1000);
// 14/3/2027 a las 9:00 en Buenos Aires.
const PARCIAL = Date.UTC(2027, 2, 14, 12) / 1000;

const HOME: HomeData = {
  exam: {
    name: "PRIMER PARCIAL", course: "Análisis Matemático", opens_at: PARCIAL, days_left: 20,
    attempts: 1, time_limit_s: 7200, max_grade: 10,
  },
  topics: [
    { id: 1, name: "Límites", mastery: 0.6449, practiced: true, seen: 3, total: 12, at: PARCIAL },
    { id: 2, name: "Derivadas", mastery: 0.8, practiced: true, seen: 9, total: 12, at: PARCIAL },
  ],
  suggestion: { id: 1, name: "Límites", mastery: 0.6449, practiced: true, seen: 3, total: 12, at: PARCIAL },
  processing: { pending: 0, done: 2, failed: 0 },
  ready: null,
};

const SESSION = {
  id: 5, course: "Análisis Matemático", topic_id: 1, topic: "Límites", exam_id: 3, starts_at: NOW + 3600,
  minutes: 40, done: false, kind: "focus" as const, topic_ids: [1], review_minutes: 0, focus_minutes: 40,
  label: "Foco: Límites",
};

const HEALTH: ExamHealth[] = [
  {
    exam_id: 3, exam: "PRIMER PARCIAL", course: "Análisis Matemático", at: PARCIAL, minutes_needed: 360,
    minutes_available: 240, verdict: "no llegás", due_reviews: 0,
    weak_topics: [
      { topic_id: 1, topic: "Límites", probability: 0.64, minutes: 200 },
      { topic_id: 2, topic: "Derivadas", probability: 0.8, minutes: 160 },
    ],
    today: SESSION, kind: "parcial", note: null, mock_suggested_at: null,
  },
];

const exam = (over: Partial<ExamInfo>): ExamInfo => ({
  id: 3, course_id: 10, course: "Análisis Matemático", name: "Primer parcial", at: PARCIAL, closes_at: null,
  source: "manual", kind: "parcial", topic_ids: [], max_grade: 10, time_limit_s: null, attempts: null, editable: true,
  result: null, result_skipped: false, ...over,
});

function renderHome(extra: Record<string, unknown> = {}, onPractice = vi.fn()) {
  const fetchMock = mockApi({
    "GET /api/home": HOME,
    "GET /api/plan/health": HEALTH,
    "GET /api/exams": [exam({})],
    "GET /api/agenda": { days: [], health: [] },
    ...extra,
  });
  render(
    <Home refreshKey={0} aiStopReason={null} working={false} onPractice={onPractice} onNavigate={() => {}} onSessionExpired={() => {}} />,
  );
  return fetchMock;
}

describe("Inicio v2", () => {
  it("Inicio muestra probabilidad y por qué del ¿llegás?", async () => {
    const onPractice = vi.fn();
    renderHome({}, onPractice);
    // La frase va una sola vez, bajo el título; cada tema dice su número.
    expect(await screen.findAllByText("Probabilidad de que lo sepas el día del parcial (14/3)")).toHaveLength(1);
    expect(screen.getByText("64 %")).toBeInTheDocument();
    expect(screen.getByText("80 %")).toBeInTheDocument();
    // Con pocas preguntas vistas, se aclara para que un número bajo no se lea mal.
    expect(screen.getByText("Viste 3 de 12 preguntas")).toBeInTheDocument();
    expect(screen.queryByText(/Viste 9 de 12/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Dominio/)).not.toBeInTheDocument();
    expect(screen.queryByText(/% bien/)).not.toBeInTheDocument();

    // El examen del título no se repite en su línea.
    expect(await screen.findByText(/^Con tu tiempo actual no llegás\./)).toBeInTheDocument();
    expect(screen.queryByText(/Primer parcial: con tu tiempo/)).not.toBeInTheDocument();
    expect(screen.getByText(/Te faltan Límites y Derivadas, unas 6 h, y tenés 4 h libres/)).toBeInTheDocument();
    expect(screen.getByText(/Hoy: 40 min de Límites/)).toBeInTheDocument();
  });

  it("muestra Hoy con la próxima clase y la sesión del día", async () => {
    const today = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Argentina/Buenos_Aires" }).format(new Date());
    const onPractice = vi.fn();
    renderHome(
      {
        "GET /api/agenda": {
          health: [],
          days: [
            {
              date: today,
              items: [
                { kind: "class", id: 1, title: "Análisis Matemático", course: "Análisis Matemático", starts_at: NOW + 1800, ends_at: NOW + 9000, place: "Aula 204" },
                { ...SESSION, kind: "session", title: "Foco: Límites", session_kind: "focus", ends_at: NOW + 6000 },
              ],
            },
          ],
        },
      },
      onPractice,
    );
    expect(await screen.findByRole("heading", { name: "Hoy" })).toBeInTheDocument();
    expect(screen.getByText(/Análisis Matemático · Aula 204/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Practicar Foco: Límites" }));
    expect(onPractice).toHaveBeenCalledWith(
      { session_id: 5 },
      { kind: "focus", topic: "Límites", review_minutes: 0, focus_minutes: 40 },
    );
  });

  it("¿Cómo te fue? aparece después del examen", async () => {
    const fetchMock = renderHome({
      "GET /api/exams": [exam({ id: 8, name: "Parcialito", at: NOW - 2 * 86400 })],
      "POST /api/exams/8/result": { grade: 7.5, max_grade: 10, at: NOW },
    });
    expect(await screen.findByText(/¿Cómo te fue en Parcialito\?/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Nota, de 0 a 10"), "7,5");
    await userEvent.click(screen.getByRole("button", { name: "Guardar nota" }));
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => url === "/api/exams/8/result");
      expect(JSON.parse(String(call![1]!.body))).toEqual({ grade: 7.5 });
    });
    await waitFor(() => expect(screen.queryByText(/¿Cómo te fue/)).not.toBeInTheDocument());
  });

  it("\"Prefiero no decir\" no la vuelve a pedir", async () => {
    const fetchMock = renderHome({
      "GET /api/exams": [exam({ id: 8, name: "Parcialito", at: NOW - 2 * 86400 })],
      "POST /api/exams/8/result/skip": { ok: true },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Prefiero no decir" }));
    expect(fetchMock).toHaveBeenCalledWith("/api/exams/8/result/skip", expect.objectContaining({ method: "POST" }));
    await waitFor(() => expect(screen.queryByText(/¿Cómo te fue/)).not.toBeInTheDocument());
  });

  it("no pregunta antes del examen, ni si ya tiene nota o se salteó", async () => {
    renderHome({
      "GET /api/exams": [
        exam({ id: 1, at: NOW + 86400 }),
        exam({ id: 2, at: NOW - 86400, result: { grade: 8, max_grade: 10, at: NOW } }),
        exam({ id: 3, at: NOW - 86400, result_skipped: true }),
      ],
    });
    await screen.findByText(/Probabilidad de que lo sepas/);
    expect(screen.queryByText(/¿Cómo te fue/)).not.toBeInTheDocument();
  });
});

describe("Inicio: ronda 1", () => {
  it("sin intentos ni límite no dice \"null intentos\"", async () => {
    renderHome({ "GET /api/home": { ...HOME, exam: { ...HOME.exam!, attempts: null, time_limit_s: 0 } } });
    await screen.findByText("Primer parcial");
    expect(screen.queryByText(/intento|null|0 minutos/)).not.toBeInTheDocument();
  });

  it("un tema sin preguntas no tiene barra", async () => {
    renderHome({
      "GET /api/home": { ...HOME, topics: [{ id: 3, name: "Integrales", mastery: 0, practiced: false, seen: 0, total: 0, at: null }] },
    });
    expect(await screen.findByText("Todavía no tiene preguntas")).toBeInTheDocument();
    expect(screen.queryByTestId("mastery-bar")).not.toBeInTheDocument();
  });

  it("con la nota guardada, el examen deja de preguntar por la nota (no queda como salteado)", async () => {
    const fetchMock = renderHome({
      "GET /api/exams": [exam({ id: 8, name: "Parcialito", at: NOW - 2 * 86400 })],
      "POST /api/exams/8/result": { grade: 7, max_grade: 10, at: NOW },
    });
    await userEvent.type(await screen.findByLabelText("Nota, de 0 a 10"), "7");
    await userEvent.click(screen.getByRole("button", { name: "Guardar nota" }));
    await waitFor(() => expect(screen.queryByText(/¿Cómo te fue/)).not.toBeInTheDocument());
    expect(fetchMock).not.toHaveBeenCalledWith("/api/exams/8/result/skip", expect.anything());
  });
});

describe("Inicio: ronda 2", () => {
  it("la fecha del encabezado es la del examen que se muestra y un tema de otro examen dice la suya", async () => {
    // 7/11/2026 a las 9:00 en Buenos Aires: otro examen, antes del parcial.
    const OTRO = Date.UTC(2026, 10, 7, 12) / 1000;
    renderHome({
      "GET /api/home": {
        ...HOME,
        topics: [
          { id: 5, name: "Rectas", mastery: 0.5, practiced: true, seen: 9, total: 9, at: OTRO },
          { id: 1, name: "Límites", mastery: 0.6449, practiced: true, seen: 9, total: 12, at: PARCIAL },
          { id: 6, name: "Conjuntos", mastery: 0.9, practiced: true, seen: 9, total: 9, at: null },
        ],
      },
    });
    expect(await screen.findByText("Probabilidad de que lo sepas el día del parcial (14/3)")).toBeInTheDocument();
    // El número va con su fecha cuando no es la del encabezado.
    expect(screen.getByText("50 % · 7/11")).toBeInTheDocument();
    expect(screen.getByText("64 %")).toBeInTheDocument();
    expect(screen.getByText("90 % · hoy")).toBeInTheDocument();
  });

  it("Hoy ordena la clase y la sesión por hora", async () => {
    const today = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Argentina/Buenos_Aires" }).format(new Date());
    renderHome({
      "GET /api/agenda": {
        health: [],
        days: [
          {
            date: today,
            items: [
              { ...SESSION, kind: "session", title: "Foco: Límites", session_kind: "focus", starts_at: NOW + 600, ends_at: NOW + 3600 },
              { kind: "class", id: 1, title: "Análisis Matemático", course: "Análisis Matemático", starts_at: NOW + 7200, ends_at: NOW + 9000, place: "Aula 204" },
            ],
          },
        ],
      },
    });
    const list = (await screen.findByRole("heading", { name: "Hoy" })).nextElementSibling!;
    expect(list.textContent!.indexOf("Foco: Límites")).toBeLessThan(list.textContent!.indexOf("Aula 204"));
  });
});

describe("Inicio 3.1", () => {
  it("Inicio ofrece practicar con 10 preguntas aunque falten clases", async () => {
    const onPractice = vi.fn();
    renderHome(
      {
        "GET /api/home": {
          ...HOME,
          processing: { pending: 3, done: 1, failed: 0 },
          ready: { course_id: 10, course: "Análisis Matemático", questions: 10 },
        },
      },
      onPractice,
    );
    expect(
      await screen.findByText("Ya hay 10 preguntas de Análisis Matemático: podés empezar mientras se preparan las demás."),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Practicar" }));
    expect(onPractice).toHaveBeenCalledWith({});
    // "Practicar" hace lo mismo que "Práctica de todo": no se ofrecen los dos.
    expect(screen.queryByRole("button", { name: "Práctica de todo" })).not.toBeInTheDocument();
  });

  it("el tope de pedidos del día no se muestra como error", async () => {
    mockApi({
      "GET /api/home": { ...HOME, processing: { pending: 3, done: 1, failed: 0 } },
      "GET /api/plan/health": [],
      "GET /api/exams": [],
      "GET /api/agenda": { days: [], health: [] },
    });
    render(
      <Home refreshKey={0} aiStopReason="budget" working={false} onPractice={vi.fn()} onNavigate={() => {}} onSessionExpired={() => {}} />,
    );
    const line = await screen.findByText("Lo de hoy ya está. Las demás clases siguen mañana.");
    expect(line).not.toHaveAttribute("role", "alert");
    expect(line.className).not.toMatch(/bad/);
    expect(screen.queryByText(/por preparar/)).not.toBeInTheDocument();
  });

  it("con el tope del día y preguntas listas, una sola línea que dice las dos cosas", async () => {
    const onPractice = vi.fn();
    mockApi({
      "GET /api/home": {
        ...HOME,
        processing: { pending: 3, done: 1, failed: 0 },
        ready: { course_id: 10, course: "Análisis Matemático", questions: 10 },
      },
      "GET /api/plan/health": [],
      "GET /api/exams": [],
      "GET /api/agenda": { days: [], health: [] },
    });
    render(
      <Home refreshKey={0} aiStopReason="budget" working={false} onPractice={onPractice} onNavigate={() => {}} onSessionExpired={() => {}} />,
    );
    expect(
      await screen.findByText("Ya hay 10 preguntas de Análisis Matemático. Las demás clases se preparan mañana."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Lo de hoy ya está/)).not.toBeInTheDocument();
    expect(screen.queryByText(/podés empezar mientras/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Practicar" }));
    expect(onPractice).toHaveBeenCalledWith({});
  });

  it("el enlace Cómo vengo lleva a su pantalla, y sin pretest no hay oferta", async () => {
    mockApi({
      "GET /api/home": HOME,
      "GET /api/plan/health": [],
      "GET /api/exams": [],
      "GET /api/agenda": { days: [], health: [] },
    });
    const onNavigate = vi.fn();
    render(
      <Home refreshKey={0} aiStopReason={null} working={false} onPractice={vi.fn()} onNavigate={onNavigate} onSessionExpired={() => {}} />,
    );
    const link = await screen.findByRole("link", { name: "Cómo vengo" });
    expect(link).toHaveAttribute("href", "#/como-vengo");
    await userEvent.click(link);
    expect(onNavigate).toHaveBeenCalledWith("como-vengo");
    expect(screen.queryByText("3 preguntas para arrancar")).not.toBeInTheDocument();
  });

  it("si llegás a todo, lo dice una vez y solo lista lo que tiene algo para hacer", async () => {
    const ok = (over: Partial<ExamHealth>): ExamHealth => ({
      ...HEALTH[0], verdict: "llegás", weak_topics: [], today: null, note: null, mock_suggested_at: null, ...over,
    });
    const onShortMock = vi.fn();
    mockApi({
      "GET /api/home": HOME,
      "GET /api/plan/health": [
        ok({ exam_id: 3 }),
        ok({ exam_id: 4, exam: "Segundo parcial", at: PARCIAL + 86400 * 30 }),
        ok({
          exam_id: 5, exam: "Final de Matemática", at: PARCIAL + 86400 * 20, kind: "final",
          note: "Faltan menos de 4 semanas: el plan usa el tiempo que hay.",
        }),
        ok({ exam_id: 6, exam: "Recuperatorio de funciones", at: PARCIAL + 86400 * 5, kind: "recuperatorio", mock_suggested_at: NOW }),
      ],
      "GET /api/exams": [],
      "GET /api/agenda": { days: [], health: [] },
    });
    render(
      <Home refreshKey={0} aiStopReason={null} working={false} onPractice={vi.fn()} onShortMock={onShortMock} onNavigate={() => {}} onSessionExpired={() => {}} />,
    );
    expect(await screen.findByText("Con tu tiempo actual llegás a todo.")).toBeInTheDocument();
    expect(screen.queryByText(/con tu tiempo actual llegás\./i)).not.toBeInTheDocument();
    // Sin acción ni nota, no hay línea.
    expect(screen.queryByText(/Segundo parcial/)).not.toBeInTheDocument();
    const line = (text: string) => (_: string, el: Element | null) => el?.tagName === "P" && el.textContent === text;
    expect(screen.getByText(line("Final de Matemática: faltan menos de 4 semanas, el plan usa el tiempo que hay."))).toBeInTheDocument();
    expect(screen.getByText(line("Recuperatorio de funciones · Hoy: simulacro corto"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Hoy: simulacro corto" }));
    expect(onShortMock).toHaveBeenCalledWith(6);
  });

  it("no pregunta cómo te fue en una inscripción", async () => {
    renderHome({
      "GET /api/exams": [
        exam({ id: 9, name: "Inscripción a finales", kind: "inscripcion", at: NOW - 3 * 86400, closes_at: NOW - 86400, max_grade: null }),
      ],
    });
    await screen.findByText(/Probabilidad de que lo sepas/);
    expect(screen.queryByText(/¿Cómo te fue/)).not.toBeInTheDocument();
  });
});
