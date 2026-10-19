import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ExamStyle from "./ExamStyle";
import type { ExamStyleData, ExamStyleView } from "../api";
import { jsonResponse, mockApi } from "../test-utils";

const PROPOSED: ExamStyleData = {
  groups: [{ kind: "opcion_multiple", count: 4, points_each: 1.5 }],
  minutes: 90,
  max_grade: 10,
  topic_ids: [1],
  topics_unmatched: [],
  formula_sheet: false,
};

const view = (over: Partial<ExamStyleView> = {}): ExamStyleView => ({
  saved: null,
  proposed: PROPOSED,
  sources: [],
  topics: [
    { id: 1, name: "Límites" },
    { id: 2, name: "Derivadas" },
  ],
  missing: 0,
  missing_text: null,
  can_generate: false,
  ai: true,
  ...over,
});

const BIBLIOGRAPHY = "Si cambió la bibliografía, revisá que los temas sigan entrando.";

describe("Formato de la cátedra", () => {
  it("el formato se puede editar y guardar", async () => {
    const fetchMock = mockApi({ "PUT /api/exams/4/style": { ok: true } });
    const onChange = vi.fn();
    render(<ExamStyle examId={4} view={view()} onChange={onChange} />);

    expect(screen.getByLabelText("Cantidad de opción múltiple")).toHaveValue("4");
    expect(screen.getByLabelText("Puntos cada uno de opción múltiple")).toHaveValue("1,5");
    expect(screen.getByLabelText("Puntos cada uno de opción múltiple")).toHaveAttribute("inputmode", "decimal");
    await userEvent.type(screen.getByLabelText("Cantidad de desarrollo"), "2");
    await userEvent.type(screen.getByLabelText("Puntos cada uno de desarrollo"), "2,5");
    await userEvent.clear(screen.getByLabelText("Tiempo (minutos)"));
    await userEvent.type(screen.getByLabelText("Tiempo (minutos)"), "120");
    await userEvent.click(screen.getByLabelText("Hay hoja de fórmulas"));
    await userEvent.click(screen.getByLabelText("Derivadas"));
    await userEvent.click(screen.getByRole("button", { name: "Usar este formato" }));

    const [, init] = fetchMock.mock.calls.find(([url]) => url === "/api/exams/4/style")!;
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(String(init!.body))).toEqual({
      groups: [
        { kind: "opcion_multiple", count: 4, points_each: 1.5 },
        { kind: "desarrollo", count: 2, points_each: 2.5 },
      ],
      minutes: 120,
      max_grade: 10,
      topic_ids: [1, 2],
      topics_unmatched: [],
      formula_sheet: true,
    });
    expect(onChange).toHaveBeenCalled();
  });

  it("mientras es propuesto dice de cuántos parciales sale, en una línea", () => {
    render(
      <ExamStyle
        examId={4}
        view={view({
          sources: [
            { past_exam_id: 1, name: "Parcial 2023.pdf", questions: 10, minutes: 120, max_grade: 10, formula_sheet: false },
            { past_exam_id: 2, name: "Foto parcial.jpg", questions: 8, minutes: 90, max_grade: 10, formula_sheet: true },
          ],
        })}
        onChange={() => {}}
      />,
    );
    expect(screen.getByText(/Sale del promedio de 2 parciales\./)).toBeInTheDocument();
    expect(screen.queryByText(/Parcial 2023\.pdf/)).not.toBeInTheDocument();
  });

  it("con un solo parcial lo dice en singular", () => {
    const one = { past_exam_id: 1, name: "Parcial 2023.pdf", questions: 10, minutes: 120, max_grade: 10, formula_sheet: false };
    render(<ExamStyle examId={4} view={view({ sources: [one] })} onChange={() => {}} />);
    expect(screen.getByText(/Sale del promedio de 1 parcial\./)).toBeInTheDocument();
  });

  it("guardado, ya no dice de dónde salió", () => {
    const one = { past_exam_id: 1, name: "Parcial 2023.pdf", questions: 10, minutes: 120, max_grade: 10, formula_sheet: false };
    render(<ExamStyle examId={4} view={view({ saved: PROPOSED, sources: [one, { ...one, past_exam_id: 2 }] })} onChange={() => {}} />);
    expect(screen.queryByText(/promedio/)).not.toBeInTheDocument();
  });

  it('lo propuesto por los parciales dice "Si cambió la bibliografía, revisá que los temas sigan entrando."', () => {
    const { unmount } = render(<ExamStyle examId={4} view={view()} onChange={() => {}} />);
    expect(screen.getByText(BIBLIOGRAPHY)).toBeInTheDocument();
    unmount();
    // Cargado a mano o ya guardado: no salió de los parciales.
    const { unmount: unmount2 } = render(<ExamStyle examId={4} view={view({ proposed: null })} onChange={() => {}} />);
    expect(screen.queryByText(BIBLIOGRAPHY)).not.toBeInTheDocument();
    unmount2();
    render(<ExamStyle examId={4} view={view({ saved: PROPOSED })} onChange={() => {}} />);
    expect(screen.queryByText(BIBLIOGRAPHY)).not.toBeInTheDocument();
  });

  it("un tema que no está en la materia dice que no entra en el simulacro", () => {
    const { unmount } = render(
      <ExamStyle examId={4} view={view({ proposed: { ...PROPOSED, topics_unmatched: ["Series"] } })} onChange={() => {}} />,
    );
    expect(screen.getByText("Series no está en la materia, así que no entra en el simulacro.")).toBeInTheDocument();
    unmount();
    render(
      <ExamStyle
        examId={4}
        view={view({ proposed: { ...PROPOSED, topics_unmatched: ["Series", "Sucesiones", "Integrales"] } })}
        onChange={() => {}}
      />,
    );
    expect(
      screen.getByText("Series, Sucesiones e Integrales no están en la materia, así que no entran en el simulacro."),
    ).toBeInTheDocument();
  });

  it("sin temas fuera de la materia no dice nada de eso", () => {
    render(<ExamStyle examId={4} view={view()} onChange={() => {}} />);
    expect(screen.queryByText(/no está en la materia|no están en la materia/)).not.toBeInTheDocument();
  });

  it('guardado, el botón dice "Guardar cambios"', () => {
    render(<ExamStyle examId={4} view={view({ saved: PROPOSED })} onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Guardar cambios" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Usar este formato" })).not.toBeInTheDocument();
  });

  it("con uno guardado ofrece volver al de siempre", async () => {
    const fetchMock = mockApi({ "DELETE /api/exams/4/style": { ok: true } });
    const onChange = vi.fn();
    render(<ExamStyle examId={4} view={view({ saved: PROPOSED })} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: "Volver al de siempre" }));
    expect(fetchMock).toHaveBeenCalledWith("/api/exams/4/style", expect.objectContaining({ method: "DELETE" }));
    expect(onChange).toHaveBeenCalled();
  });

  it("sin uno guardado no ofrece volver al de siempre", () => {
    render(<ExamStyle examId={4} view={view()} onChange={() => {}} />);
    expect(screen.queryByRole("button", { name: "Volver al de siempre" })).not.toBeInTheDocument();
  });

  it("un número fuera de rango se dice tranquilo, sin guardar", async () => {
    mockApi({
      "PUT /api/exams/4/style": () => jsonResponse(422, { detail: [{ msg: "Input should be less than or equal to 100" }] }),
    });
    render(<ExamStyle examId={4} view={view()} onChange={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Usar este formato" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Revisá los números");
    expect(alert).not.toHaveTextContent(/Input should/);
  });

  it("un 400 muestra el texto del servidor", async () => {
    mockApi({ "PUT /api/exams/4/style": () => jsonResponse(400, { detail: "El examen necesita al menos un ejercicio" }) });
    render(<ExamStyle examId={4} view={view({ proposed: null })} onChange={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Usar este formato" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El examen necesita al menos un ejercicio");
    // Los temas se eligen con casillas.
    expect(within(screen.getByRole("group", { name: "Temas" })).getAllByRole("checkbox")).toHaveLength(2);
  });
});
