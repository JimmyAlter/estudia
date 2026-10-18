import { act, render, screen } from "@testing-library/react";
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
