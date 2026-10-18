import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import Materials from "./Materials";
import type { Material, Section } from "../api";
import { mockApi } from "../test-utils";

const SUMMARY = "# Rectas\n\n## Pendiente\n\nCuánto sube por cada paso.\n\n## Ordenada al origen\n\nDonde corta al eje y.";

const material: Material = {
  id: 4, title: "Video-clase 1: Rectas", kind: "youtube", status: "done", summary_md: SUMMARY, error: null,
  youtube_id: null, campus_url: null, topic_ids: [], questions: 0,
};

const SECTIONS: Section[] = [{ id: 1, course_id: 1, num: 1, name: "Semana 1", course: "Análisis", materials: [material] }];

afterEach(() => {
  vi.useRealTimers();
});

describe("Contá lo que te acordás", () => {
  it("Contá lo que te acordás marca los títulos que no mencionaste", async () => {
    const fetchMock = mockApi({
      "GET /api/materials": SECTIONS,
      "POST /api/materials/4/recall": {
        headings: [
          { title: "Rectas", mentioned: true },
          { title: "Pendiente", mentioned: true },
          { title: "Ordenada al origen", mentioned: false },
        ],
      },
    });
    render(<Materials aiStopReason={null} />);
    const recallButton = await screen.findByRole("button", { name: "Contá lo que te acordás" });
    // Va antes de "Ver resumen".
    const summaryButton = screen.getByRole("button", { name: "Ver resumen" });
    expect(recallButton.compareDocumentPosition(summaryButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await userEvent.click(recallButton);
    expect(screen.getByText("3 min")).toBeInTheDocument();
    // El resumen no se ve mientras se escribe, ni se ofrece abrirlo o cerrarlo: Saltear y Terminar alcanzan.
    expect(screen.queryByText("Cuánto sube por cada paso.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resumen/ })).not.toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox"), "La pendiente es cuánto sube");
    await userEvent.click(screen.getByRole("button", { name: "Terminar" }));

    expect(await screen.findByText("Cuánto sube por cada paso.")).toBeInTheDocument();
    expect(screen.getByText("La pendiente es cuánto sube")).toBeInTheDocument();
    // Con el resultado a la vista, vuelve el botón para cerrarlo.
    expect(screen.getByRole("button", { name: "Ocultar resumen" })).toBeInTheDocument();
    // Solo el título que faltó lleva la marca.
    expect(screen.getAllByText("No lo mencionaste")).toHaveLength(1);
    const missed = screen.getByRole("heading", { name: /Ordenada al origen/ });
    expect(missed).toHaveTextContent("No lo mencionaste");
    expect(screen.getByRole("heading", { name: "Pendiente" })).not.toHaveTextContent("No lo mencionaste");

    const [, init] = fetchMock.mock.calls.find(([url]) => url === "/api/materials/4/recall")!;
    expect(JSON.parse(String(init!.body))).toEqual({ text: "La pendiente es cuánto sube" });
  });

  it("el reloj de 3 minutos se puede saltear", async () => {
    const fetchMock = mockApi({ "GET /api/materials": SECTIONS });
    render(<Materials aiStopReason={null} />);
    const recallButton = await screen.findByRole("button", { name: "Contá lo que te acordás" });

    vi.useFakeTimers();
    fireEvent.click(recallButton);
    // Solo minutos, sin cuenta regresiva por segundo.
    expect(screen.getByText("3 min")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(screen.getByText("3 min")).toBeInTheDocument();
    expect(screen.queryByText(/:\d\d/)).not.toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(31_000);
    });
    expect(screen.getByText("2 min")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByText("1 min")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.queryByText(/^\d min$/)).not.toBeInTheDocument();
    // Al llegar a 0 no corta: avisa y se puede seguir.
    expect(screen.getByText("Pasaron 3 minutos. Podés seguir o terminar.")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Saltear" }));
    vi.useRealTimers();
    // Saltear abre el resumen sin comparar nada.
    await waitFor(() => expect(screen.getByText("Cuánto sube por cada paso.")).toBeInTheDocument());
    expect(screen.queryByText("No lo mencionaste")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/recall"))).toBe(false);
  });
});
