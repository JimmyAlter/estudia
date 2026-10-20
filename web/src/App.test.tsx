import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { POLL_IDLE_MS, POLL_OFFLINE_MS } from "./App";
import type { AppState } from "./api";
import { jsonResponse, mockApi } from "./test-utils";

const READY: AppState = {
  step: "listo",
  site_id: "utn-frba",
  ai_stop_reason: null,
  syncing: false,
  last_sync_at: 1_790_000_000,
  last_sync_error: null, demo: false,
};

const HOME = {
  exam: null,
  topics: [],
  suggestion: null,
  processing: { pending: 1, done: 2, failed: 0 },
};

const flush = () => act(() => vi.advanceTimersByTimeAsync(0));

afterEach(() => {
  vi.useRealTimers();
  window.location.hash = "";
});

describe("App", () => {
  it("sigue consultando el estado después de un error y se recupera sola", async () => {
    vi.useFakeTimers();
    const replies: (AppState | Error)[] = [READY, new Error("caído"), READY];
    const fetchMock = mockApi({
      "GET /api/state": () => {
        const next = replies.shift() ?? READY;
        if (next instanceof Error) throw next;
        return next;
      },
      "GET /api/home": HOME,
    });
    const stateCalls = () => fetchMock.mock.calls.filter(([url]) => url === "/api/state").length;

    render(<App />);
    await flush();
    expect(screen.getByText("Sin exámenes próximos")).toBeInTheDocument();
    expect(stateCalls()).toBe(1);

    await act(() => vi.advanceTimersByTimeAsync(POLL_IDLE_MS));
    expect(stateCalls()).toBe(2);
    expect(screen.getByText("Estudia no responde. Se reintenta solo.")).toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(POLL_OFFLINE_MS));
    expect(stateCalls()).toBe(3);
    expect(screen.queryByText("Estudia no responde. Se reintenta solo.")).not.toBeInTheDocument();
    expect(screen.getByText("Sin exámenes próximos")).toBeInTheDocument();
  });

  it("una instalación sin respuesta de edad la pregunta arriba de Inicio, en una línea", async () => {
    const fetchMock = mockApi({
      "GET /api/state": { ...READY, age: null },
      "GET /api/home": HOME,
      "PUT /api/settings": { ok: true },
    });
    render(<App />);
    expect(await screen.findByText(/¿Tenés 18 años o más\?/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "No" }));
    expect(fetchMock).toHaveBeenCalledWith("/api/settings", expect.objectContaining({ method: "PUT" }));
  });

  it("con la edad respondida no hay pregunta en Inicio", async () => {
    mockApi({ "GET /api/state": { ...READY, age: "minor" }, "GET /api/home": HOME });
    render(<App />);
    await screen.findByText("Sin exámenes próximos");
    expect(screen.queryByText(/¿Tenés 18 años o más\?/)).not.toBeInTheDocument();
  });

  it("avisa una sola vez que se terminó lo gratis de Gemini", async () => {
    mockApi({ "GET /api/state": { ...READY, ai_stop_reason: "quota" }, "GET /api/home": HOME });
    render(<App />);
    expect(
      await screen.findByText("Por hoy se usó todo lo gratis de Gemini. Mañana se preparan las clases que faltan."),
    ).toBeInTheDocument();
    await screen.findByText("Sin exámenes próximos");
    expect(screen.queryByText(/Preparando/)).not.toBeInTheDocument();
  });

  it("avisa una sola vez que se llegó al tope de videos de YouTube", async () => {
    mockApi({ "GET /api/state": { ...READY, ai_stop_reason: "youtube_cap" }, "GET /api/home": HOME });
    render(<App />);
    expect(
      await screen.findByText("Por hoy se llegó al tope de videos de YouTube. Mañana se preparan las clases que faltan."),
    ).toBeInTheDocument();
    await screen.findByText("Sin exámenes próximos");
    expect(screen.queryByText(/Preparando|por preparar/)).not.toBeInTheDocument();
  });

  it("con la sesión vencida mantiene el inicio y ofrece volver a entrar", async () => {
    const fetchMock = mockApi({
      "GET /api/state": { ...READY, step: "login" },
      "GET /api/home": HOME,
      "POST /api/login/start": { url: "https://campus/launch" },
    });
    render(<App />);
    expect(
      await screen.findByText(/Tu sesión del campus venció. No llega material nuevo hasta que vuelvas a entrar./),
    ).toBeInTheDocument();
    expect(await screen.findByText("Sin exámenes próximos")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Volver a entrar" }));
    expect(
      await screen.findByText(/Iniciá sesión en la ventana que se abrió. Cuando termines, volvé acá./),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/login/start", expect.objectContaining({ method: "POST" }));
  });

  it("con la sesión vencida también se puede pegar el enlace a mano", async () => {
    const PASTED = "moodlemobile://token=c2VjcmV0bw==";
    let loggedIn = false;
    const fetchMock = mockApi({
      "GET /api/state": () => (loggedIn ? READY : { ...READY, step: "login" }),
      "GET /api/home": HOME,
      "POST /api/login/start": { url: "https://campus/launch" },
      "POST /api/login/callback": () => {
        loggedIn = true;
        return { ok: true };
      },
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Volver a entrar" }));

    const field = await screen.findByLabelText(
      "¿No volvió solo? Pegá acá el enlace que empieza con moodlemobile:// o estudia://",
    );
    await userEvent.type(field, PASTED);
    await userEvent.click(screen.getByRole("button", { name: "Continuar" }));

    await waitFor(() =>
      expect(screen.queryByText(/Tu sesión del campus venció/)).not.toBeInTheDocument(),
    );
    expect(screen.queryByLabelText(/No volvió solo/)).not.toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([url]) => url === "/api/login/callback");
    expect(JSON.parse(String(call![1]!.body))).toEqual({ url: PASTED });
  });

  it("un 401 del inicio lleva al aviso para volver a entrar", async () => {
    const states = [READY, { ...READY, step: "login" as const }];
    mockApi({
      "GET /api/state": () => states.shift() ?? { ...READY, step: "login" },
      "GET /api/home": () => jsonResponse(401, { detail: "hay que iniciar sesión en Moodle" }),
    });
    render(<App />);
    expect(await screen.findByRole("button", { name: "Volver a entrar" })).toBeInTheDocument();
  });

  it("muestra el aviso cuando hay versión nueva", async () => {
    const url = "https://github.com/JimmyAlter/estudia/releases/tag/v0.2.0";
    mockApi({
      "GET /api/state": READY,
      "GET /api/home": HOME,
      "GET /api/version": { current: "0.1.0", latest: { version: "0.2.0", url } },
    });
    render(<App />);
    expect(await screen.findByText(/Hay una versión nueva \(0\.2\.0\)\./)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Descargar" });
    expect(link).toHaveAttribute("href", url);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("no muestra nada sin versión nueva", async () => {
    const fetchMock = mockApi({
      "GET /api/state": READY,
      "GET /api/home": HOME,
      "GET /api/version": { current: "0.1.0", latest: null },
    });
    render(<App />);
    await screen.findByText("Sin exámenes próximos");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/version", expect.anything()));
    expect(screen.queryByText(/versión nueva/)).not.toBeInTheDocument();
    // La versión instalada no ocupa el pie de cada pantalla: está en Ajustes.
    expect(screen.queryByText("Estudia 0.1.0")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Descargar" })).not.toBeInTheDocument();
  });

  it("si falla la consulta de versión no muestra errores", async () => {
    mockApi({ "GET /api/state": READY, "GET /api/home": HOME });
    render(<App />);
    await screen.findByText("Sin exámenes próximos");
    expect(screen.queryByText(/versión nueva/)).not.toBeInTheDocument();
    expect(screen.queryByText(/error/i)).not.toBeInTheDocument();
  });
});

describe("App: navegación", () => {
  it("al cambiar de pantalla el foco va al título", async () => {
    mockApi({
      "GET /api/state": READY,
      "GET /api/home": HOME,
      "GET /api/agenda": { days: [], health: [] },
    });
    render(<App />);
    await screen.findByText("Sin exámenes próximos");
    await userEvent.click(screen.getByRole("link", { name: "Agenda" }));
    const title = await screen.findByRole("heading", { level: 1, name: "Agenda" });
    await waitFor(() => expect(title).toHaveFocus());
  });

  it("el botón del simulacro corto lleva al simulacro corto de ese examen", async () => {
    vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    const now = Math.floor(Date.now() / 1000);
    const retake = {
      exam_id: 7, exam: "Recuperatorio del primer parcial", course: "Análisis Matemático", at: now + 10 * 86400,
      minutes_needed: 60, minutes_available: 300, verdict: "llegás", due_reviews: 0, weak_topics: [], today: null,
      kind: "recuperatorio", note: null, mock_suggested_at: now,
    };
    const fetchMock = mockApi({
      "GET /api/state": READY,
      "GET /api/home": HOME,
      "GET /api/agenda": { days: [], health: [retake] },
      "GET /api/exams": [
        {
          id: 7, course_id: 10, course: "Análisis Matemático", name: "Recuperatorio del primer parcial",
          at: now + 10 * 86400, closes_at: null, source: "manual", kind: "recuperatorio", topic_ids: [1],
          max_grade: 10, time_limit_s: null, attempts: null, editable: true, result: null, result_skipped: false,
        },
      ],
      "POST /api/mock/start": () => new Response(JSON.stringify({ detail: "x" }), { status: 500 }),
    });
    render(<App />);
    await screen.findByText("Sin exámenes próximos");
    await userEvent.click(screen.getByRole("link", { name: "Agenda" }));
    await userEvent.click(await screen.findByRole("button", { name: "Hoy: simulacro corto" }));
    expect(await screen.findByText("Simulacro corto")).toBeInTheDocument();
    expect(window.location.hash).toBe("#/simulacro");
    await userEvent.click(screen.getByRole("button", { name: "Empezar" }));
    const call = fetchMock.mock.calls.find(([url]) => url === "/api/mock/start");
    expect(JSON.parse(String(call![1]!.body))).toEqual({ exam_id: 7, predicted_grade: null, short: true });
    vi.restoreAllMocks();
  });
});


describe("App: materia de ejemplo", () => {
  const FIRST: AppState = { ...READY, step: "facultad", site_id: null, last_sync_at: null, demo: false };
  const DEMO: AppState = { ...FIRST, demo: true };
  const NOW = Math.floor(Date.now() / 1000);
  const ITEM = {
    question_id: 1, stem: "¿Cuál es la pendiente de y = 3x + 2?", options: ["1", "2", "3", "0"],
    topic: "Funciones lineales", format: "mcq", phase: "focus", relearning: null,
  };
  const FEEDBACK = {
    correct: true, correct_index: 2, chosen_index: 2, explanation: "En y = mx + b la pendiente es m.",
    common_error: null, source_seconds: null, youtube_id: null, typed_ok: null, requeue: false,
    next_label: "Vuelve en 3 días", relearn_progress: null, feedback_style: "short", answer_id: 5,
  };
  const EXAM = {
    id: 1, course_id: 1, course: "Matemática de ejemplo", name: "Parcial de ejemplo", at: NOW + 7 * 86400,
    closes_at: null, source: "manual", kind: "parcial", topic_ids: [1, 2], max_grade: 10, time_limit_s: null,
    attempts: null, editable: true, result: null, result_skipped: false,
  };
  beforeEach(() => {
    vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  const STUDY_ROUTES = [
    /^\/api\/state$/,
    /^\/api\/version$/,
    /^\/api\/demo$/,
    /^\/api\/quiz$/,
    /^\/api\/answer$/,
    /^\/api\/progress$/,
    /^\/api\/exams$/,
    /^\/api\/exams\/\d+\/topics$/,
    /^\/api\/mock\//,
  ];

  function demoApi(initial: AppState) {
    let state = initial;
    return mockApi({
      "GET /api/state": () => state,
      "GET /api/catalog": [{ id: "utn-frba", name: "Facultad Regional Buenos Aires", university: "UTN" }],
      "POST /api/demo": () => {
        state = { ...state, demo: true };
        return { ok: true };
      },
      "POST /api/quiz": [ITEM],
      "POST /api/answer": FEEDBACK,
      "GET /api/progress": { calibration: { total: 1, missing: 9, levels: [] }, predictions: [], gap: null },
      "GET /api/exams": [EXAM],
      "POST /api/mock/start": { attempt_id: 3, items: [ITEM], seconds: 90, max_grade: 10, predicted_grade: null },
    });
  }

  it("al probar la materia de ejemplo aparece la primera pregunta", async () => {
    const fetchMock = demoApi(FIRST);
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Probar con una materia de ejemplo" }));
    expect(await screen.findByText("¿Cuál es la pendiente de y = 3x + 2?")).toBeInTheDocument();
    expect(screen.getByText("Estás en la materia de ejemplo")).toBeInTheDocument();
    expect(window.location.hash).toBe("#/practica");
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/home")).toBe(false);
  });

  it("en la materia de ejemplo se ve la franja y solo Inicio y Práctica", async () => {
    demoApi(DEMO);
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "Matemática de ejemplo" })).toBeInTheDocument();
    expect(screen.getByText("Estás en la materia de ejemplo")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Conectar mi campus" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Secciones" });
    expect([...nav.querySelectorAll("a")].map((a) => a.textContent)).toEqual(["Inicio", "Práctica"]);
    for (const name of ["Agenda", "Materiales", "Ajustes"]) expect(screen.queryByText(name)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Actualizar/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/¿Tenés 18 años o más\?/)).not.toBeInTheDocument();
  });

  it("en la materia de ejemplo una dirección que no es de estudio muestra el inicio", async () => {
    window.location.hash = "#/agenda";
    const fetchMock = demoApi(DEMO);
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "Matemática de ejemplo" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith("/api/agenda"))).toBe(false);
  });

  it("en la materia de ejemplo Cómo vengo no se abre: muestra el inicio", async () => {
    window.location.hash = "#/como-vengo";
    const fetchMock = demoApi(DEMO);
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "Matemática de ejemplo" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith("/api/progress"))).toBe(false);
  });

  it("en el simulacro de la materia de ejemplo no se pide la nota que creés", async () => {
    demoApi(DEMO);
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Hacer un simulacro corto" }));
    expect(await screen.findByText("Simulacro corto")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Empezar" })).toBeInTheDocument();
    expect(screen.queryByLabelText("¿Cuánto creés que te sacás?")).not.toBeInTheDocument();
  });

  it('"Conectar mi campus" lleva al paso de la facultad y se puede volver', async () => {
    demoApi(DEMO);
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Conectar mi campus" }));
    expect(screen.getByRole("heading", { name: "¿En qué facultad estudiás?" })).toBeInTheDocument();
    // Ya está en la materia de ejemplo: no se la vuelve a ofrecer.
    expect(screen.queryByRole("button", { name: "Probar con una materia de ejemplo" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Volver al ejemplo" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Matemática de ejemplo" })).toBeInTheDocument();
  });

  it("en la materia de ejemplo solo se piden rutas de estudio", async () => {
    const fetchMock = demoApi(DEMO);
    render(<App />);

    // Práctica con una respuesta.
    await userEvent.click(await screen.findByRole("button", { name: "Practicar" }));
    await screen.findByText("¿Cuál es la pendiente de y = 3x + 2?");
    await userEvent.click(screen.getByRole("button", { name: /^3/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Seguro/ }));
    expect(await screen.findByText(/En y = mx \+ b/)).toBeInTheDocument();

    // Cómo vengo no está en la demo.
    await userEvent.click(screen.getByRole("link", { name: "Inicio" }));
    await screen.findByRole("button", { name: "Hacer un simulacro corto" });
    expect(screen.queryByRole("link", { name: "Cómo vengo" })).not.toBeInTheDocument();

    // Un simulacro corto empezado.
    await userEvent.click(screen.getByRole("link", { name: "Inicio" }));
    await userEvent.click(await screen.findByRole("button", { name: "Hacer un simulacro corto" }));
    expect(await screen.findByText("Simulacro corto")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Empezar" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === "/api/mock/start")).toBe(true));
    expect(await screen.findByText("¿Cuál es la pendiente de y = 3x + 2?")).toBeInTheDocument();

    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls.filter((url) => !STUDY_ROUTES.some((route) => route.test(url)))).toEqual([]);
  });
});
