import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App, { POLL_IDLE_MS, POLL_OFFLINE_MS } from "./App";
import type { AppState } from "./api";
import { jsonResponse, mockApi } from "./test-utils";

const READY: AppState = {
  step: "listo",
  site_id: "utn-frba",
  ai_stop_reason: null,
  syncing: false,
  last_sync_at: 1_790_000_000,
  last_sync_error: null,
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

  it("avisa una sola vez que se terminó lo gratis de Gemini", async () => {
    mockApi({ "GET /api/state": { ...READY, ai_stop_reason: "quota" }, "GET /api/home": HOME });
    render(<App />);
    expect(
      await screen.findByText("Por hoy se usó todo lo gratis de Gemini. Mañana se preparan las clases que faltan."),
    ).toBeInTheDocument();
    await screen.findByText("Sin exámenes próximos");
    expect(screen.queryByText(/Preparando/)).not.toBeInTheDocument();
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
    expect(await screen.findByText("Estudia 0.1.0")).toBeInTheDocument();
    expect(await screen.findByText(/Hay una versión nueva \(0\.2\.0\)\./)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Descargar" });
    expect(link).toHaveAttribute("href", url);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("no muestra nada sin versión nueva", async () => {
    mockApi({
      "GET /api/state": READY,
      "GET /api/home": HOME,
      "GET /api/version": { current: "0.1.0", latest: null },
    });
    render(<App />);
    expect(await screen.findByText("Estudia 0.1.0")).toBeInTheDocument();
    expect(screen.queryByText(/versión nueva/)).not.toBeInTheDocument();
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
