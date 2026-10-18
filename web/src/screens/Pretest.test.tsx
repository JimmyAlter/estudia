import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Home from "./Home";
import Pretest from "./Pretest";
import type { Feedback, HomeData, QuizItem } from "../api";
import { mockApi } from "../test-utils";

const OFFER = { material_id: 9, title: "Video-clase 3: Rectas", course: "Análisis Matemático" };

const HOME: HomeData = {
  exam: null,
  topics: [],
  suggestion: null,
  processing: { pending: 0, done: 1, failed: 0 },
  ready: null,
  pretest: OFFER,
};

const ITEMS: QuizItem[] = [
  { question_id: 1, stem: "¿Pendiente de y = 2x + 1?", options: ["1", "2", "3"], topic: "Rectas", format: "mcq", phase: "focus", relearning: null },
  { question_id: 2, stem: "¿Ordenada de y = 2x + 1?", options: ["1", "2", "3"], topic: "Rectas", format: "mcq", phase: "focus", relearning: null },
  { question_id: 3, stem: "¿Recta paralela a y = 2x?", options: ["y = 2x + 5", "y = x", "y = -2x"], topic: "Rectas", format: "mcq", phase: "focus", relearning: null },
];

const feedback = (question_id: number, correct_index: number, chosen_index: number): Feedback => ({
  correct: correct_index === chosen_index,
  correct_index,
  chosen_index,
  explanation: `Explicación ${question_id}: en y = mx + b, m es la pendiente.`,
  common_error: null,
  source_seconds: null,
  youtube_id: null,
  typed_ok: null,
  requeue: false,
  next_label: null,
  relearn_progress: null,
  feedback_style: "full",
  question_id,
});

function pretestRoutes() {
  return {
    "GET /api/home": HOME,
    "POST /api/pretest/9/start": ITEMS,
    // Contesta "mal" la primera: siempre corrige con la opción que dice el servidor.
    "POST /api/pretest/answer": (body: unknown) => {
      const { question_id, chosen_index, shown_order } = body as { question_id: number; chosen_index: number; shown_order: number[] };
      const correct = question_id === 3 ? 0 : 1;
      return feedback(question_id, correct, shown_order[chosen_index]);
    },
    "POST /api/pretest/9/close": { ok: true },
  };
}

describe("3 preguntas para arrancar", () => {
  it('Inicio ofrece 3 preguntas para arrancar con "Es normal no saberlas todavía"', async () => {
    const fetchMock = mockApi({
      ...pretestRoutes(),
      "GET /api/plan/health": [],
      "GET /api/exams": [],
      "GET /api/agenda": { days: [], health: [] },
    });
    const onNavigate = vi.fn();
    render(
      <Home refreshKey={0} aiStopReason={null} working={false} onPractice={() => {}} onNavigate={onNavigate} onSessionExpired={() => {}} />,
    );
    expect(await screen.findByRole("heading", { name: "3 preguntas para arrancar" })).toBeInTheDocument();
    expect(screen.getByText("De Video-clase 3: Rectas. No cuentan: es para que la clase te rinda más.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Empezar" }));
    expect(onNavigate).toHaveBeenCalledWith("arrancar");
    // Empezar no cierra la oferta: eso pasa al terminar.
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/pretest/9/close")).toBe(false);

    render(<Pretest onNavigate={onNavigate} />);
    expect(await screen.findByText("Es normal no saberlas todavía")).toBeInTheDocument();
    expect(await screen.findByText(/Pendiente de y = 2x/)).toBeInTheDocument();
  });

  it("Ahora no cierra la oferta y la saca del inicio", async () => {
    const fetchMock = mockApi({
      ...pretestRoutes(),
      "GET /api/plan/health": [],
      "GET /api/exams": [],
      "GET /api/agenda": { days: [], health: [] },
    });
    render(
      <Home refreshKey={0} aiStopReason={null} working={false} onPractice={() => {}} onNavigate={() => {}} onSessionExpired={() => {}} />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Ahora no" }));
    await waitFor(() => expect(screen.queryByText("3 preguntas para arrancar")).not.toBeInTheDocument());
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/pretest/9/close")).toHaveLength(1);
  });

  it("las preguntas para arrancar no muestran puntaje", async () => {
    const fetchMock = mockApi(pretestRoutes());
    const onNavigate = vi.fn();
    render(<Pretest onNavigate={onNavigate} />);
    expect(await screen.findByText("Es normal no saberlas todavía")).toBeInTheDocument();

    // Primera: elige "3", la correcta es "2".
    await userEvent.click(await screen.findByRole("button", { name: /^3/ }));
    expect(await screen.findByText("Explicación 1: en y = mx + b, m es la pendiente.")).toBeInTheDocument();
    // La correcta es la que dijo el servidor, y la elegida no se marca como correcta.
    expect(screen.getByRole("button", { name: /^2.*Correcta/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^3.*Tu respuesta/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^3.*Correcta/ })).not.toBeInTheDocument();
    expect(screen.getByText("Es normal no saberlas todavía")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Siguiente" }));

    await userEvent.click(await screen.findByRole("button", { name: /^2/ }));
    await screen.findByText(/Explicación 2/);
    await userEvent.click(screen.getByRole("button", { name: "Siguiente" }));

    await userEvent.click(await screen.findByRole("button", { name: /^y\s=\s2x\s\+\s5/ }));
    await screen.findByText(/Explicación 3/);
    await userEvent.click(screen.getByRole("button", { name: "Terminar" }));

    expect(await screen.findByText("Listo. Ahora mirá la clase: te va a rendir más.")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => url === "/api/pretest/9/close")).toHaveLength(1));
    // Sin puntaje en ningún momento: ni "de 3", ni "Bien" o "Mal", ni porcentajes.
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/\bde 3\b|\bBien\b|\bMal\b|%|puntaje|Correctas|Incorrecta/);

    // Las respuestas mandan la posición tocada dentro del orden mostrado.
    const bodies = fetchMock.mock.calls
      .filter(([url]) => url === "/api/pretest/answer")
      .map(([, init]) => JSON.parse(String(init!.body)));
    expect(bodies).toHaveLength(3);
    expect(bodies[0].shown_order[bodies[0].chosen_index]).toBe(2);
    expect(bodies[0]).not.toHaveProperty("confidence");

    await userEvent.click(screen.getByRole("button", { name: "Ver la clase" }));
    expect(onNavigate).toHaveBeenCalledWith("materiales");
  });
});
