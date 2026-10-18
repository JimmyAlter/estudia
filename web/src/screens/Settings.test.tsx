import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Settings from "./Settings";
import { jsonResponse, mockApi } from "../test-utils";

const SCHEDULE = {
  slots: [{ id: 1, course_id: 10, course: "Análisis Matemático", weekday: 0, start_min: 510, end_min: 630, place: "Aula 204" }],
  suggestions: [{ course_id: 20, course_name: "AM1 - 8.30 HS", start_min: 510, end_min: 630 }],
};

function routes(extra: Record<string, unknown> = {}) {
  return mockApi({
    "GET /api/schedule": SCHEDULE,
    "GET /api/courses": [
      { id: 10, name: "Análisis Matemático" },
      { id: 20, name: "AM1 - 8.30 HS" },
    ],
    "GET /api/plan/settings": { availability: [60, 60, 60, 60, 60, 0, 0], study_start_min: 1140 },
    "GET /api/version": { current: "0.2.0", latest: null },
    "GET /api/settings": { reminders: true, age: "adult", allow_download: false },
    ...extra,
  });
}

const bodyOf = (fetchMock: ReturnType<typeof mockApi>, url: string, method = "POST") =>
  JSON.parse(String(fetchMock.mock.calls.find(([u, init]) => u === url && init?.method === method)![1]!.body));

describe("Ajustes", () => {
  it("muestra las secciones", async () => {
    routes();
    render(<Settings />);
    expect(await screen.findByRole("heading", { name: "Horarios de cursada" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Tiempo para estudiar" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Recordatorios" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Edad" })).toBeInTheDocument();
    expect(screen.getByText("Funcionan con Estudia abierta.")).toBeInTheDocument();
    expect(await screen.findByText(/Lunes, 8:30 a 10:30/)).toBeInTheDocument();
    expect(screen.queryByText(/Sugerencias, por el nombre/)).not.toBeInTheDocument();
    // La versión instalada vive al pie de Ajustes.
    expect(await screen.findByText("Estudia 0.2.0")).toBeInTheDocument();
  });

  it("Ajustes cambia la edad", async () => {
    const fetchMock = routes({ "PUT /api/settings": { ok: true } });
    const onChange = vi.fn();
    render(<Settings onChange={onChange} />);
    expect(await screen.findByRole("heading", { name: "Edad" })).toBeInTheDocument();
    const adult = screen.getByRole("radio", { name: "Tengo 18 años o más" });
    await waitFor(() => expect(adult).toBeEnabled());
    expect(adult).toBeChecked();
    await userEvent.click(screen.getByRole("radio", { name: "Tengo menos de 18" }));
    await waitFor(() => expect(bodyOf(fetchMock, "/api/settings", "PUT")).toEqual({ age: "minor" }));
    expect(screen.getByRole("radio", { name: "Tengo menos de 18" })).toBeChecked();
    // Una sola lectura de los ajustes para toda la pantalla.
    expect(fetchMock.mock.calls.filter(([u, i]) => u === "/api/settings" && (i?.method ?? "GET") === "GET")).toHaveLength(1);
    expect(onChange).toHaveBeenCalled();
  });

  it("Ajustes guarda la disponibilidad", async () => {
    const fetchMock = routes({ "PUT /api/plan/settings": { ok: true } });
    render(<Settings />);
    const saturday = await screen.findByLabelText("Minutos el sábado");
    await userEvent.clear(saturday);
    await userEvent.type(saturday, "120");
    const start = screen.getByLabelText("Hora preferida para empezar");
    await userEvent.clear(start);
    await userEvent.type(start, "18:30");
    await userEvent.click(screen.getByRole("button", { name: "Guardar tiempo" }));

    await waitFor(() =>
      expect(bodyOf(fetchMock, "/api/plan/settings", "PUT")).toEqual({
        availability: [60, 60, 60, 60, 60, 120, 0],
        study_start_min: 1110,
      }),
    );
    expect(await screen.findByText("Guardado")).toBeInTheDocument();
  });

  it("Sugerencia de horario se puede agregar y descartar", async () => {
    let schedule = structuredClone(SCHEDULE);
    const fetchMock = routes({
      "GET /api/schedule": () => schedule,
      "POST /api/schedule": { id: 2 },
      "POST /api/schedule/suggestions/20/dismiss": () => {
        schedule = { ...schedule, suggestions: [] };
        return { ok: true };
      },
    });
    render(<Settings />);
    const suggestion = await screen.findByText(/^AM1, 8:30 a 10:30, sin día$/);
    const row = suggestion.closest("li")!;

    // Agregar abre el alta con el curso y la hora puestos; falta el día.
    await userEvent.click(within(row).getByRole("button", { name: "Agregar" }));
    const form = await screen.findByRole("form", { name: "Horario" });
    expect(within(form).getByLabelText("Materia")).toHaveValue("20");
    expect(within(form).getByLabelText("Desde")).toHaveValue("08:30");
    await userEvent.selectOptions(within(form).getByLabelText("Día"), "2");
    await userEvent.click(within(form).getByRole("button", { name: "Guardar horario" }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, "/api/schedule")).toEqual({
        course_id: 20, weekday: 2, start_min: 510, end_min: 630, place: null,
      }),
    );

    await userEvent.click(within((await screen.findByText(/^AM1, 8:30 a 10:30, sin día$/)).closest("li")!).getByRole("button", { name: "Descartar" }));
    await waitFor(() => expect(screen.queryByText(/^AM1, 8:30 a 10:30, sin día$/)).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith("/api/schedule/suggestions/20/dismiss", expect.objectContaining({ method: "POST" }));
  });

  it("el interruptor de recordatorios guarda al tocarlo", async () => {
    const fetchMock = routes({ "PUT /api/settings": { ok: true } });
    render(<Settings />);
    const toggle = await screen.findByRole("switch", { name: "Recordatorios" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await userEvent.click(toggle);
    await waitFor(() => expect(bodyOf(fetchMock, "/api/settings", "PUT")).toEqual({ reminders: false }));
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });
});

describe("Ajustes: ronda 1", () => {
  it("las horas por semana van con coma decimal", async () => {
    routes({ "GET /api/plan/settings": { availability: [105, 105, 105, 105, 105, 0, 0], study_start_min: 1140 } });
    render(<Settings />);
    expect(await screen.findByText("8,8 h por semana")).toBeInTheDocument();
  });
});

describe("Ajustes: videos de YouTube", () => {
  it("la descarga viene apagada y se prende al tocarla", async () => {
    const fetchMock = routes({ "PUT /api/settings": { ok: true } });
    render(<Settings />);
    expect(await screen.findByRole("heading", { name: "Videos de YouTube" })).toBeInTheDocument();
    const toggle = screen.getByRole("switch", { name: "Descargar los videos cuando el link no alcanza" });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("Bajarlos usa más internet y lugar en el disco. Viene apagado.")).toBeInTheDocument();
    await userEvent.click(toggle);
    await waitFor(() => expect(bodyOf(fetchMock, "/api/settings", "PUT")).toEqual({ allow_download: true }));
    expect(toggle).toHaveAttribute("aria-checked", "true");
  });

  it("si no se puede guardar vuelve atrás y avisa", async () => {
    routes({ "PUT /api/settings": () => jsonResponse(500, { detail: "x" }) });
    render(<Settings />);
    const toggle = await screen.findByRole("switch", { name: "Descargar los videos cuando el link no alcanza" });
    await waitFor(() => expect(toggle).toBeEnabled());
    await userEvent.click(toggle);
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo guardar. Probá de nuevo.");
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });
});
