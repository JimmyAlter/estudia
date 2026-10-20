import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import DemoHome from "./DemoHome";
import Onboarding from "./Onboarding";
import type { AppState, ExamInfo } from "../api";
import { jsonResponse, mockApi } from "../test-utils";

const FIRST_STEP: AppState = {
  step: "facultad", site_id: null, ai_stop_reason: null, syncing: false,
  last_sync_at: null, last_sync_error: null, demo: false,
};

const NOW = Math.floor(Date.now() / 1000);
const EXAM: ExamInfo = {
  id: 1, course_id: 1, course: "Matemática de ejemplo", name: "Parcial de ejemplo", at: NOW + 7 * 86400,
  closes_at: null, source: "manual", kind: "parcial", topic_ids: [1, 2], max_grade: 10, time_limit_s: null,
  attempts: null, editable: true, result: null, result_skipped: false,
};

describe("Materia de ejemplo: el primer paso", () => {
  it('el primer paso ofrece "Probar con una materia de ejemplo"', async () => {
    const fetchMock = mockApi({ "GET /api/catalog": [], "POST /api/demo": { ok: true } });
    const onDemo = vi.fn();
    render(<Onboarding state={FIRST_STEP} onChange={() => {}} onDemo={onDemo} />);
    expect(screen.getByRole("heading", { name: "¿En qué facultad estudiás?" })).toBeInTheDocument();
    expect(
      screen.getByText("Sin campus, sin clave y sin internet. Lo que contestes queda aparte de tus materias."),
    ).toBeInTheDocument();
    // Antes de tocarlo no se abre nada.
    expect(fetchMock).not.toHaveBeenCalledWith("/api/demo", expect.anything());

    await userEvent.click(screen.getByRole("button", { name: "Probar con una materia de ejemplo" }));
    expect(fetchMock).toHaveBeenCalledWith("/api/demo", expect.objectContaining({ method: "POST" }));
    expect(onDemo).toHaveBeenCalledTimes(1);
  });

  it("si no se puede abrir la materia de ejemplo lo dice y se puede reintentar", async () => {
    mockApi({ "GET /api/catalog": [], "POST /api/demo": () => jsonResponse(409, { detail: "x" }) });
    const onDemo = vi.fn();
    render(<Onboarding state={FIRST_STEP} onChange={() => {}} onDemo={onDemo} />);
    await userEvent.click(screen.getByRole("button", { name: "Probar con una materia de ejemplo" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo abrir la materia de ejemplo. Probá de nuevo.");
    expect(onDemo).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Probar con una materia de ejemplo" })).toBeEnabled();
  });

  it("sin onDemo no se ofrece la materia de ejemplo", () => {
    mockApi({ "GET /api/catalog": [] });
    render(<Onboarding state={FIRST_STEP} onChange={() => {}} />);
    expect(screen.queryByRole("button", { name: "Probar con una materia de ejemplo" })).not.toBeInTheDocument();
  });
});

describe("DemoHome", () => {
  it("DemoHome ofrece practicar y el simulacro corto, nada más", async () => {
    mockApi({ "GET /api/exams": [{ ...EXAM, id: 9, kind: "inscripcion" }, EXAM] });
    const onPractice = vi.fn();
    const onShortMock = vi.fn();
    render(<DemoHome onPractice={onPractice} onShortMock={onShortMock} />);

    // El banner ya dice que es de ejemplo: no se repite arriba del título.
    expect(screen.queryByText("Materia de ejemplo")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Matemática de ejemplo" })).toBeInTheDocument();
    expect(screen.getByText("2 temas y 12 preguntas, escritas para probar Estudia.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Practicar" }));
    expect(onPractice).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Hacer un simulacro corto" }));
    // Una inscripción no es un examen: el corto es del parcial.
    expect(onShortMock).toHaveBeenCalledWith(1);

    // Sin historia que mostrar, "Cómo vengo" no tiene lugar en la demo.
    expect(screen.queryByRole("link", { name: "Cómo vengo" })).not.toBeInTheDocument();
  });

  it("si no se puede leer el examen, el simulacro corto lo dice", async () => {
    mockApi({ "GET /api/exams": () => jsonResponse(500, { detail: "x" }) });
    const onShortMock = vi.fn();
    render(<DemoHome onPractice={() => {}} onShortMock={onShortMock} />);
    await userEvent.click(screen.getByRole("button", { name: "Hacer un simulacro corto" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo abrir el simulacro. Probá de nuevo.");
    expect(onShortMock).not.toHaveBeenCalled();
  });
});
