import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import Mock from "./Mock";
import type { ExamInfo, Feedback, MockResult, MockStart, QuizItem } from "../api";
import { jsonResponse, mockApi } from "../test-utils";

const NOW = Math.floor(Date.now() / 1000);

const EXAM: ExamInfo = {
  id: 4,
  course_id: 10,
  course: "Análisis Matemático",
  name: "Primer parcial",
  at: NOW + 10 * 86400,
  closes_at: null,
  source: "manual",
  kind: "parcial",
  topic_ids: [1, 2],
  max_grade: 10,
  time_limit_s: null,
  attempts: null,
  editable: true,
  result: null,
  result_skipped: false,
};

const q = (id: number, over: Partial<QuizItem> = {}): QuizItem => ({
  question_id: id,
  stem: `Pregunta ${id}`,
  options: ["a", "b", "c"],
  topic: id === 1 ? "Límites" : "Derivadas",
  format: "mcq",
  phase: "focus",
  relearning: null,
  ...over,
});

const START: MockStart = {
  attempt_id: 9,
  items: [q(1), q(2, { format: "typed" })],
  seconds: 180,
  max_grade: 10,
  predicted_grade: 7,
};

const fb = (over: Partial<Feedback>): Feedback => ({
  correct: true,
  correct_index: 0,
  chosen_index: 0,
  explanation: "Porque sí.",
  common_error: null,
  source_seconds: null,
  youtube_id: null,
  typed_ok: null,
  requeue: false,
  next_label: null,
  relearn_progress: null,
  feedback_style: "full",
  ...over,
});

const RESULT: MockResult = {
  grade: 5,
  max_grade: 10,
  correct: 1,
  total: 2,
  failed_topics: ["Derivadas"],
  review: [
    fb({ question_id: 1 }),
    fb({ question_id: 2, correct: false, chosen_index: null, typed_ok: false, explanation: "Derivá bien." }),
  ],
  late: false,
  predicted_grade: 7,
};

afterEach(() => vi.useRealTimers());

function routes(extra: Record<string, unknown> = {}) {
  return mockApi({
    "GET /api/exams": [EXAM],
    "POST /api/mock/start": START,
    "POST /api/mock/9/finish": RESULT,
    "GET /api/exams/4/topics": [
      { id: 1, name: "Límites" },
      { id: 2, name: "Derivadas" },
    ],
    ...extra,
  });
}

const bodyOf = (fetchMock: ReturnType<typeof mockApi>, url: string) =>
  JSON.parse(String(fetchMock.mock.calls.find(([u]) => u === url)![1]!.body));

describe("Simulacro", () => {
  it("Simulacro pide predicción y la muestra en el resultado", async () => {
    const fetchMock = routes();
    render(<Mock onPractice={() => {}} onExit={() => {}} />);

    expect(await screen.findByText("No vas a ver las respuestas hasta el final")).toBeInTheDocument();
    const prediction = screen.getByLabelText("¿Cuánto creés que te sacás?");
    await userEvent.type(prediction, "7");
    await userEvent.click(screen.getByRole("button", { name: "Empezar" }));
    expect(bodyOf(fetchMock, "/api/mock/start")).toEqual({ exam_id: 4, predicted_grade: 7 });

    await screen.findByText("Pregunta 1");
    await userEvent.click(screen.getByRole("button", { name: /^a/ }));
    await userEvent.click(screen.getByRole("button", { name: "Siguiente" }));
    // Las escritas usan el mismo campo que la práctica.
    await userEvent.type(screen.getByLabelText("Tu respuesta"), "2x");
    await userEvent.click(screen.getByRole("button", { name: "Terminar" }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, terminar" }));

    expect(await screen.findByText(/Creías que te sacabas 7/)).toBeInTheDocument();
    expect(screen.getByText("Tus errores vuelven mañana en la práctica.")).toBeInTheDocument();
    expect(screen.getByText("1 de 2 bien")).toBeInTheDocument();
    expect(screen.getByText("Fallaste en: Derivadas")).toBeInTheDocument();
    expect(bodyOf(fetchMock, "/api/mock/9/finish")).toEqual({ answers: { 1: { index: 0 }, 2: { typed: "2x" } } });
  });

  it("una inscripción no se ofrece para simulacro", async () => {
    routes({
      "GET /api/exams": [
        { ...EXAM, id: 6, name: "Inscripción a finales", kind: "inscripcion", topic_ids: [], max_grade: null, closes_at: NOW + 12 * 86400 },
        EXAM,
      ],
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    expect(await screen.findByText("No vas a ver las respuestas hasta el final")).toBeInTheDocument();
    expect(screen.queryByText(/Inscripción a finales/)).not.toBeInTheDocument();
  });

  it("el recuperatorio sugiere un simulacro corto de 6 preguntas", async () => {
    const RETAKE: ExamInfo = { ...EXAM, id: 7, name: "Recuperatorio del primer parcial", kind: "recuperatorio", time_limit_s: 7200 };
    const fetchMock = routes({ "GET /api/exams": [EXAM, RETAKE] });
    render(<Mock start={{ examId: 7, short: true }} onPractice={() => {}} onExit={() => {}} />);

    expect(await screen.findByText("Simulacro corto")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Recuperatorio del primer parcial");
    // No se elige examen: es el del recuperatorio. Y su reloj es el de 6 preguntas, no el del examen.
    expect(screen.queryByText("¿De qué examen?")).not.toBeInTheDocument();
    expect(screen.getByText(/6 preguntas de los temas que todavía no tenés firmes/)).toBeInTheDocument();
    // El tiempo por pregunta va en su propio renglón, no colgado de un punto.
    const perQuestion = screen.getByText("90 segundos por pregunta");
    expect(perQuestion).toHaveClass("block");
    expect(perQuestion.parentElement!.textContent).not.toMatch(/·\s*90 segundos/);
    await userEvent.click(screen.getByRole("button", { name: "Empezar" }));
    expect(bodyOf(fetchMock, "/api/mock/start")).toEqual({ exam_id: 7, predicted_grade: null, short: true });
  });

  it("en la materia de ejemplo no se pide la nota que creés", async () => {
    const fetchMock = routes({ "GET /api/exams": [EXAM] });
    render(<Mock start={{ examId: EXAM.id, short: true }} demo onPractice={() => {}} onExit={() => {}} />);
    const begin = await screen.findByRole("button", { name: "Empezar" });
    expect(screen.queryByLabelText("¿Cuánto creés que te sacás?")).not.toBeInTheDocument();
    expect(screen.queryByText(/Si no sabés, dejalo vacío/)).not.toBeInTheDocument();
    await userEvent.click(begin);
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === "/api/mock/start")).toBe(true));
    expect(bodyOf(fetchMock, "/api/mock/start")).toMatchObject({ predicted_grade: null });
  });

  it("sin preguntas de los temas flojos, el simulacro corto lo dice con calma", async () => {
    const RETAKE: ExamInfo = { ...EXAM, id: 7, name: "Recuperatorio del primer parcial", kind: "recuperatorio" };
    const text = "Todavía no hay suficientes preguntas de los temas flojos para un simulacro corto.";
    routes({ "GET /api/exams": [RETAKE], "POST /api/mock/start": () => jsonResponse(409, { detail: text }) });
    render(<Mock start={{ examId: 7, short: true }} onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    const line = await screen.findByText(text);
    expect(line).not.toHaveClass("text-bad");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("la predicción se puede saltar", async () => {
    const fetchMock = routes();
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    expect(bodyOf(fetchMock, "/api/mock/start")).toEqual({ exam_id: 4, predicted_grade: null });
  });

  it("Simulacro no muestra correcciones antes de terminar", async () => {
    routes();
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    await userEvent.click(screen.getByRole("button", { name: /^b/ }));
    expect(screen.queryByText(/Correcta|correcta|Incorrecta|Porque sí/)).not.toBeInTheDocument();
    // Se puede volver y cambiar, desde el mapa de preguntas.
    await userEvent.click(screen.getByRole("button", { name: "Siguiente" }));
    await userEvent.click(screen.getByRole("button", { name: /Ir a la pregunta 1/ }));
    expect(screen.getByRole("button", { name: /^b/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("Simulacro termina solo al vencer el tiempo", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchMock = routes();
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    expect(screen.getByText("03:00")).toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(181_000));
    expect(await screen.findByText("1 de 2 bien")).toBeInTheDocument();
    expect(bodyOf(fetchMock, "/api/mock/9/finish")).toEqual({ answers: {} });
  });

  it("Practicar lo que fallé practica los temas fallados", async () => {
    routes();
    const onPractice = vi.fn();
    render(<Mock onPractice={onPractice} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    await userEvent.click(screen.getByRole("button", { name: "Terminar" }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, terminar" }));
    await userEvent.click(await screen.findByRole("button", { name: "Practicar lo que fallé" }));
    expect(onPractice).toHaveBeenCalledWith({ topic_ids: [2] });
  });
});

describe("Simulacro: ronda 1", () => {
  it("antes de empezar dice el tiempo por pregunta y no promete cuántas", async () => {
    routes();
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    expect(await screen.findByText(/90 segundos por pregunta/)).toBeInTheDocument();
    expect(screen.queryByText(/preguntas ·|hasta \d+ preguntas/i)).not.toBeInTheDocument();
    expect(
      screen.getByText("Podés ir y volver entre las preguntas. Cuando se termina el tiempo, se entrega solo."),
    ).toBeInTheDocument();
  });

  it("la revisión se arma por pregunta: si una se retiró, se ven las demás", async () => {
    routes({
      "POST /api/mock/start": { ...START, items: [q(1), q(2), q(3)] },
      "POST /api/mock/9/finish": RESULT,
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    await userEvent.click(screen.getByRole("button", { name: "Terminar" }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, terminar" }));
    await screen.findByText("1 de 2 bien");
    const review = screen.getByRole("heading", { name: "Revisión" }).parentElement!;
    expect(review).toHaveTextContent("Pregunta 1");
    expect(review).toHaveTextContent("Pregunta 2");
    expect(review).not.toHaveTextContent("Pregunta 3");
    // Lo no respondido dice "sin responder", sin marcarlo además como incorrecta.
    expect(review).toHaveTextContent("Tu respuesta: sin responder");
    expect(review).not.toHaveTextContent("(incorrecta)");
    // La explicación de una bien respondida queda plegada.
    expect(screen.getByText("Porque sí.")).not.toBeVisible();
    expect(screen.getByText("Derivá bien.")).toBeVisible();
  });

  it("si la entrega ya se había hecho, muestra el resultado guardado", async () => {
    routes({
      "POST /api/mock/9/finish": () => jsonResponse(409, { detail: "Este simulacro ya terminó" }),
      "GET /api/mock/9": RESULT,
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    await userEvent.click(screen.getByRole("button", { name: "Terminar" }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, terminar" }));
    expect(await screen.findByText("1 de 2 bien")).toBeInTheDocument();
  });

  it("vencido el tiempo no se puede seguir respondiendo aunque falle la entrega", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    routes({ "POST /api/mock/9/finish": () => jsonResponse(503, { detail: "No pude corregir ahora, probá de nuevo" }) });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    await act(() => vi.advanceTimersByTimeAsync(181_000));
    expect(await screen.findByRole("alert")).toHaveTextContent("No pude corregir ahora");
    expect(screen.getByRole("button", { name: /^a/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Entregar de nuevo" })).toBeEnabled();
  });
});

describe("Simulacro: ronda 2", () => {
  it("una respuesta elegida y mal se marca incorrecta", async () => {
    routes({
      "POST /api/mock/9/finish": {
        ...RESULT,
        review: [fb({ question_id: 1, correct: false, correct_index: 1, chosen_index: 0 }), RESULT.review[1]],
      },
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    await userEvent.click(screen.getByRole("button", { name: /^a/ }));
    await userEvent.click(screen.getByRole("button", { name: "Terminar" }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, terminar" }));
    const review = (await screen.findByRole("heading", { name: "Revisión" })).parentElement!;
    expect(review).toHaveTextContent("Tu respuesta: a (incorrecta)");
  });

  it("con varios exámenes, la línea del tiempo no repite la materia", async () => {
    routes({ "GET /api/exams": [EXAM, { ...EXAM, id: 5, name: "Segundo parcial" }] });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    const line = await screen.findByText(/90 segundos por pregunta/);
    expect(line.textContent).toBe("90 segundos por pregunta");
  });
});

describe("Simulacro: cita de un apunte", () => {
  it("la revisión del simulacro cita la página del apunte", async () => {
    routes({
      "POST /api/mock/9/finish": {
        ...RESULT,
        review: [
          fb({ question_id: 1 }),
          fb({
            question_id: 2, correct: false, chosen_index: null, typed_ok: false, explanation: "Derivá bien.",
            source_page: 14, source_label: "Apunte 3, pág. 14",
            source_url: "https://campus.example/mod/resource/view.php?id=31",
          }),
        ],
      },
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    await userEvent.click(screen.getByRole("button", { name: "Terminar" }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, terminar" }));

    const link = await screen.findByRole("link", { name: "Apunte 3, pág. 14" });
    expect(link).toHaveAttribute("href", "https://campus.example/mod/resource/view.php?id=31");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });
});

describe("Simulacro con el formato de la cátedra", () => {
  const SAVED = {
    groups: [
      { kind: "opcion_multiple", count: 6, points_each: 1 },
      { kind: "desarrollo", count: 2, points_each: 2 },
    ],
    minutes: 90,
    max_grade: 10,
    topic_ids: [1, 2],
    topics_unmatched: [],
    formula_sheet: false,
  };
  const styleView = (over: Record<string, unknown> = {}) => ({
    saved: SAVED,
    proposed: null,
    sources: [],
    topics: [
      { id: 1, name: "Límites" },
      { id: 2, name: "Derivadas" },
    ],
    missing: 0,
    missing_text: null,
    can_generate: true,
    ai: true,
    ...over,
  });
  const NOT_READY = "Todavía faltan 3 preguntas con el formato de tu cátedra. Se preparan solas.";

  it("el simulacro con formato muestra cantidad, tiempo y escala", async () => {
    const fetchMock = routes({ "GET /api/exams/4/style": styleView(), "GET /api/exams/4/past-exams": [] });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    expect(await screen.findByText("8 preguntas")).toBeInTheDocument();
    const line = screen.getByText("8 preguntas").parentElement!;
    expect(line).toHaveTextContent("8 preguntas · 1 h 30 min · sobre 10");
    expect(screen.queryByText(/90 segundos por pregunta/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Empezar" }));
    expect(bodyOf(fetchMock, "/api/mock/start")).toEqual({ exam_id: 4, predicted_grade: null, style: true });
  });

  it("si faltan preguntas ofrece el simulacro de siempre", async () => {
    const fetchMock = routes({
      "GET /api/exams/4/style": styleView({ missing: 3, missing_text: NOT_READY }),
      "GET /api/exams/4/past-exams": [],
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    expect(await screen.findByText(NOT_READY)).toBeInTheDocument();
    // Una sola salida principal: el de siempre. La línea de arriba describe ese, no el del formato.
    expect(screen.queryByRole("button", { name: "Empezar" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hacer el simulacro de siempre" })).toHaveClass("bg-ink");
    expect(screen.getByRole("button", { name: "Volver" })).toHaveClass("text-muted");
    expect(screen.queryByText("8 preguntas")).not.toBeInTheDocument();
    expect(screen.getByText("90 segundos por pregunta")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Hacer el simulacro de siempre" }));
    expect(bodyOf(fetchMock, "/api/mock/start")).toEqual({ exam_id: 4, predicted_grade: null });
    expect(await screen.findByText("Pregunta 1")).toBeInTheDocument();
  });

  it("el aviso de las que faltan es el texto del servidor, tal cual", async () => {
    const fromServer = "Todavía falta 1 pregunta con el formato de tu cátedra. Texto del servidor.";
    routes({ "GET /api/exams/4/style": styleView({ missing: 1, missing_text: fromServer }), "GET /api/exams/4/past-exams": [] });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    expect(await screen.findByText(fromServer)).toHaveAttribute("role", "status");
    expect(screen.queryByText(/Se preparan solas/)).not.toBeInTheDocument();
  });

  it("sin clases preparadas de esos temas lo dice y ofrece el de siempre", async () => {
    routes({
      "GET /api/exams/4/style": styleView({ missing: 3, missing_text: NOT_READY, can_generate: false }),
      "GET /api/exams/4/past-exams": [],
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    expect(await screen.findByText("Faltan clases preparadas de estos temas para armar las preguntas que faltan.")).toBeInTheDocument();
    expect(screen.queryByText(NOT_READY)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hacer el simulacro de siempre" })).toBeInTheDocument();
  });

  it("si al empezar todavía faltan, lo dice tranquilo y ofrece el de siempre", async () => {
    routes({
      "GET /api/exams/4/style": styleView(),
      "GET /api/exams/4/past-exams": [],
      "POST /api/mock/start": (body: unknown) =>
        (body as { style?: boolean }).style ? jsonResponse(409, { detail: NOT_READY }) : START,
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    const notice = await screen.findByText(NOT_READY);
    expect(notice).not.toHaveClass("text-bad");
    expect(screen.queryByRole("button", { name: "Empezar" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Hacer el simulacro de siempre" }));
    expect(await screen.findByText("Pregunta 1")).toBeInTheDocument();
  });

  it("la pantalla de antes muestra los parciales y el formato, pero no en el corto", async () => {
    routes({ "GET /api/exams/4/style": styleView({ saved: null, proposed: SAVED }), "GET /api/exams/4/past-exams": [] });
    const { unmount } = render(<Mock onPractice={() => {}} onExit={() => {}} />);
    expect(await screen.findByRole("heading", { name: "Cargá tus parciales viejos" })).toBeVisible();
    expect(screen.getByText("Si cambió la bibliografía, revisá que los temas sigan entrando.")).toBeVisible();
    // Sin formato guardado no hay nada que plegar.
    expect(screen.queryByText("Cambiar el formato o cargar parciales")).not.toBeInTheDocument();
    unmount();

    render(<Mock onPractice={() => {}} onExit={() => {}} start={{ examId: 4, short: true }} />);
    expect(await screen.findByText("Simulacro corto")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Cargá tus parciales viejos" })).not.toBeInTheDocument();
    expect(screen.queryByText("8 preguntas")).not.toBeInTheDocument();
  });

  it("con un formato guardado, los parciales y el editor van plegados", async () => {
    routes({ "GET /api/exams/4/style": styleView(), "GET /api/exams/4/past-exams": [] });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    const summary = await screen.findByText("Cambiar el formato o cargar parciales");
    const details = summary.closest("details")!;
    expect(details.open).toBe(false);
    // Lo principal sigue arriba, a la vista.
    expect(screen.getByRole("button", { name: "Empezar" })).toBeVisible();

    await userEvent.click(summary);
    expect(details.open).toBe(true);
    expect(await screen.findByRole("heading", { name: "Cargá tus parciales viejos" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Volver al de siempre" })).toBeInTheDocument();
  });

  it("sin IA no ofrece cargar parciales: solo cambiar el formato", async () => {
    routes({ "GET /api/exams/4/style": styleView({ ai: false }), "GET /api/exams/4/past-exams": [] });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    const summary = await screen.findByText("Cambiar el formato");
    await userEvent.click(summary);
    expect(screen.getByRole("heading", { name: "Formato de tu cátedra" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Cargá tus parciales viejos" })).not.toBeInTheDocument();
    expect(screen.queryByText("Elegir archivo")).not.toBeInTheDocument();
  });

  it("sin IA y sin formato guardado tampoco aparece la carga de parciales", async () => {
    routes({ "GET /api/exams/4/style": styleView({ ai: false, saved: null }), "GET /api/exams/4/past-exams": [] });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    expect(await screen.findByRole("heading", { name: "Formato de tu cátedra" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Cargá tus parciales viejos" })).not.toBeInTheDocument();
  });

  it("si todas valen lo mismo, no se dicen los puntos", async () => {
    routes({
      "POST /api/mock/start": { ...START, items: [q(1, { points: 2 }), q(2, { points: 2 })] },
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    expect(screen.queryByText("2 puntos")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Terminar" }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, terminar" }));
    await screen.findByRole("heading", { name: "Revisión" });
    expect(screen.queryByText(/puntos/)).not.toBeInTheDocument();
  });

  it("cada pregunta muestra sus puntos", async () => {
    routes({
      "POST /api/mock/start": { ...START, items: [q(1, { points: 1 }), q(2, { points: 2.5 })] },
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    expect(screen.getByText("1 punto")).toHaveClass("text-sm", "text-muted");
    await userEvent.click(screen.getByRole("button", { name: "Siguiente" }));
    expect(await screen.findByText("2,5 puntos")).toBeInTheDocument();
  });

  it("la revisión del simulacro con formato dice cuánto valía cada pregunta", async () => {
    routes({
      "POST /api/mock/start": { ...START, items: [q(1, { points: 1 }), q(2, { points: 2.5 })] },
      "POST /api/mock/9/finish": {
        ...RESULT,
        review: [fb({ question_id: 1 }), fb({ question_id: 2, correct: false, chosen_index: null, typed_ok: false })],
      },
    });
    render(<Mock onPractice={() => {}} onExit={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Empezar" }));
    await screen.findByText("Pregunta 1");
    await userEvent.click(screen.getByRole("button", { name: "Terminar" }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, terminar" }));
    await screen.findByRole("heading", { name: "Revisión" });
    expect(screen.getByText(/^1 · .* · 1 punto$/)).toBeInTheDocument();
    expect(screen.getByText(/^2 · .* · 2,5 puntos$/)).toBeInTheDocument();
  });
});
