import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Progress from "./Progress";
import type { ProgressData } from "../api";
import { mockApi } from "../test-utils";

describe("Cómo vengo", () => {
  it("Cómo vengo dice cuántas respuestas faltan", async () => {
    const data: ProgressData = { calibration: { total: 12, missing: 8, levels: [] }, predictions: [], gap: null };
    mockApi({ "GET /api/progress": data });
    render(<Progress />);
    expect(await screen.findByRole("heading", { name: "Cómo vengo", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Faltan 8 respuestas con confianza para calcularlo.")).toBeInTheDocument();
    expect(screen.queryByText(/Cuando decís/)).not.toBeInTheDocument();
  });

  it('Cómo vengo muestra "Cuando decís «Seguro», acertás 71 %"', async () => {
    const data: ProgressData = {
      calibration: {
        total: 31,
        missing: 0,
        levels: [
          { confidence: 2, label: "Seguro", answers: 14, hit_rate: 10 / 14 },
          { confidence: 1, label: "Me parece", answers: 12, hit_rate: 0.5 },
          { confidence: 0, label: "Adiviné", answers: 5, hit_rate: 0.2 },
        ],
      },
      predictions: [{ exam_id: 3, exam: "Primer parcial", predicted: 8, mock_grade: 6.5, real: 6, max_grade: 10 }],
      gap: 2,
    };
    mockApi({ "GET /api/progress": data });
    render(<Progress />);
    expect(await screen.findByText("Cuando decís «Seguro», acertás 71 %")).toBeInTheDocument();
    expect(screen.getByText("Cuando decís «Me parece», acertás 50 %")).toBeInTheDocument();
    expect(screen.getByText("Cuando decís «Adiviné», acertás 20 %")).toBeInTheDocument();
    expect(screen.getByText("Esperabas 8, el simulacro dio 6,5, sacaste 6.")).toBeInTheDocument();
    // Con una sola predicción, el promedio sería la misma cuenta: no se dice.
    expect(screen.queryByText(/En promedio/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Faltan/)).not.toBeInTheDocument();
  });

  it("con dos predicciones o más, dice cuánto le errás en promedio", async () => {
    const data: ProgressData = {
      calibration: { total: 31, missing: 0, levels: [] },
      predictions: [
        { exam_id: 3, exam: "Primer parcial", predicted: 8, mock_grade: 6.5, real: 6, max_grade: 10 },
        { exam_id: 4, exam: "Segundo parcial", predicted: 7, mock_grade: 7, real: 7, max_grade: 10 },
        { exam_id: 5, exam: "Parcialito", predicted: null, mock_grade: 5, real: 6, max_grade: 10 },
      ],
      gap: 1,
    };
    mockApi({ "GET /api/progress": data });
    render(<Progress />);
    expect(await screen.findByText("En promedio le errás por 1 punto sobre 10.")).toBeInTheDocument();
  });

  it("una predicción y otro simulacro sin predicción no alcanzan para el promedio", async () => {
    const data: ProgressData = {
      calibration: { total: 31, missing: 0, levels: [] },
      predictions: [
        { exam_id: 3, exam: "Primer parcial", predicted: 8, mock_grade: 6.5, real: 6, max_grade: 10 },
        { exam_id: 5, exam: "Parcialito", predicted: null, mock_grade: 5, real: 6, max_grade: 10 },
      ],
      gap: 2,
    };
    mockApi({ "GET /api/progress": data });
    render(<Progress />);
    await screen.findByText("Esperabas 8, el simulacro dio 6,5, sacaste 6.");
    expect(screen.queryByText(/En promedio/)).not.toBeInTheDocument();
  });
});
