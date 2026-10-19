import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import PastExams from "./PastExams";
import type { PastExam } from "../api";
import { jsonResponse, mockApi } from "../test-utils";

const row = (over: Partial<PastExam> = {}): PastExam => ({
  id: 1,
  name: "Parcial 2023.pdf",
  kind: "pdf",
  status: "done",
  error: null,
  created_at: 1_700_000_000,
  ...over,
});

afterEach(() => vi.useRealTimers());

describe("Cargá tus parciales viejos", () => {
  it("Cargá tus parciales viejos acepta un archivo y no tiene campo de URL", async () => {
    let list: PastExam[] = [];
    let uploaded: unknown = null;
    const fetchMock = mockApi({
      "GET /api/exams/4/past-exams": () => list,
      "POST /api/exams/4/past-exams": (body: unknown) => {
        uploaded = body;
        list = [row({ id: 7, name: "parcial-2024.pdf", status: "pending" })];
        return { id: 7, name: "parcial-2024.pdf", status: "pending" };
      },
    });
    const onChange = vi.fn();
    const { container } = render(<PastExams examId={4} ai onChange={onChange} />);

    expect(await screen.findByRole("heading", { name: "Cargá tus parciales viejos" })).toBeInTheDocument();
    expect(screen.getByText("PDF o foto, desde tu compu. Quedan solo en esta compu y nunca se comparten.")).toBeInTheDocument();
    const input = screen.getByLabelText("Elegir archivo");
    expect(input).toHaveAttribute("type", "file");
    expect(input).toHaveAttribute("accept", "application/pdf,image/jpeg,image/png");
    expect(container.querySelector("input[type=url]")).toBeNull();
    expect(container.textContent).not.toMatch(/URL|enlace/i);

    const file = new File(["%PDF-1.4"], "parcial-2024.pdf", { type: "application/pdf" });
    await userEvent.upload(input, file);

    expect(await screen.findByText("parcial-2024.pdf")).toBeInTheDocument();
    expect(screen.getByText("Leyendo el formato…")).toBeInTheDocument();
    expect(uploaded).toBeInstanceOf(FormData);
    expect((uploaded as FormData).get("file")).toBeInstanceOf(File);
    // El navegador pone el Content-Type con el límite: no se pone a mano.
    const [, init] = fetchMock.mock.calls.find(([url, i]) => url === "/api/exams/4/past-exams" && i?.method === "POST")!;
    expect(init?.headers).toBeUndefined();
    expect(onChange).toHaveBeenCalled();
  });

  it("un parcial que no se pudo leer lo dice sin alarma", async () => {
    mockApi({ "GET /api/exams/4/past-exams": [row({ status: "failed", error: "No se pudo leer" })] });
    render(<PastExams examId={4} ai />);
    const status = await screen.findByText("No se pudo leer: probá otra foto");
    expect(status).not.toHaveClass("text-bad");
    expect(status).toHaveClass("text-muted");
    expect(screen.getByRole("button", { name: "Quitar Parcial 2023.pdf" })).toBeInTheDocument();
  });

  it("sin IA, lo pendiente se carga a mano", async () => {
    mockApi({ "GET /api/exams/4/past-exams": [row({ status: "pending" })] });
    render(<PastExams examId={4} ai={false} />);
    expect(await screen.findByText("Sin IA: cargá el formato a mano")).toBeInTheDocument();
    expect(screen.queryByText("Leyendo el formato…")).not.toBeInTheDocument();
  });

  it("un error del servidor se muestra tal cual, en su lugar", async () => {
    mockApi({
      "GET /api/exams/4/past-exams": [],
      "POST /api/exams/4/past-exams": () =>
        jsonResponse(413, { detail: "Ese archivo pesa más de 20 MB. Probá con una foto de cada hoja." }),
    });
    render(<PastExams examId={4} ai />);
    await userEvent.upload(await screen.findByLabelText("Elegir archivo"), new File(["x"], "grande.pdf", { type: "application/pdf" }));
    const error = await screen.findByText("Ese archivo pesa más de 20 MB. Probá con una foto de cada hoja.");
    // Dice qué hacer, sin rojo.
    expect(error).toHaveClass("text-ink");
    expect(error).not.toHaveClass("text-bad");
  });

  it("si el revisor está ocupado lo dice tranquilo", async () => {
    mockApi({
      "GET /api/exams/4/past-exams": [],
      "POST /api/exams/4/past-exams": () =>
        jsonResponse(503, { detail: "Ahora no se pudo revisar el archivo. Probá de nuevo en un rato." }),
    });
    render(<PastExams examId={4} ai />);
    await userEvent.upload(await screen.findByLabelText("Elegir archivo"), new File(["x"], "a.jpg", { type: "image/jpeg" }));
    const busy = await screen.findByText("Ahora no se pudo revisar el archivo. Probá de nuevo en un rato.");
    expect(busy).toHaveClass("text-muted");
  });

  it("con 5 cargados no ofrece elegir otro", async () => {
    mockApi({ "GET /api/exams/4/past-exams": [1, 2, 3, 4, 5].map((id) => row({ id, name: `P${id}.pdf` })) });
    render(<PastExams examId={4} ai />);
    expect(await screen.findByText("Ya cargaste 5 parciales para este examen.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Elegir archivo")).not.toBeInTheDocument();
  });

  it("Quitar borra el parcial", async () => {
    let list = [row()];
    const fetchMock = mockApi({
      "GET /api/exams/4/past-exams": () => list,
      "DELETE /api/exams/4/past-exams/1": () => {
        list = [];
        return { ok: true };
      },
    });
    render(<PastExams examId={4} ai />);
    await userEvent.click(await screen.findByRole("button", { name: "Quitar Parcial 2023.pdf" }));
    expect(fetchMock).toHaveBeenCalledWith("/api/exams/4/past-exams/1", expect.objectContaining({ method: "DELETE" }));
    expect(screen.queryByText("Parcial 2023.pdf")).not.toBeInTheDocument();
  });

  it("mientras lee consulta cada 5 s y para al desmontarse", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status: PastExam["status"] = "pending";
    const fetchMock = mockApi({ "GET /api/exams/4/past-exams": () => [row({ status })] });
    const { unmount } = render(<PastExams examId={4} ai />);
    expect(await screen.findByText("Leyendo el formato…")).toBeInTheDocument();
    status = "done";
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(await screen.findByText("Listo")).toBeInTheDocument();
    const calls = () => fetchMock.mock.calls.filter(([url]) => url === "/api/exams/4/past-exams").length;
    const after = calls();
    // Sin nada pendiente ya no consulta; y desmontado tampoco.
    await act(() => vi.advanceTimersByTimeAsync(10000));
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(calls()).toBe(after);
  });
});
