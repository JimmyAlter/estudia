import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import Phone from "./Phone";
import { jsonResponse, mockApi } from "../test-utils";

const FEED_URL = "https://campus.example/calendar/export_execute.php?userid=7&authtoken=secreto";
const CAMPUS_CALENDAR = "https://campus.example/calendar/export.php";

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  return writeText;
}

afterEach(() => {
  // @ts-expect-error: se saca el stub para el próximo test.
  delete navigator.clipboard;
});

describe("En el celular", () => {
  it("la guía del calendario muestra el enlace solo al pedirlo y lo copia", async () => {
    const writeText = stubClipboard();
    const log = vi.spyOn(console, "log");
    const fetchMock = mockApi({
      "GET /api/calendar-feed": { status: "saved", campus_calendar_url: CAMPUS_CALENDAR },
      "POST /api/calendar-feed/reveal": { url: FEED_URL },
    });
    const { unmount } = render(<Phone />);

    const show = await screen.findByRole("button", { name: "Mostrar enlace" });
    // Antes de pedirlo, el enlace no se pide ni se muestra.
    expect(fetchMock).not.toHaveBeenCalledWith("/api/calendar-feed/reveal", expect.anything());
    expect(screen.queryByDisplayValue(FEED_URL)).not.toBeInTheDocument();
    expect(screen.getByText("Copiá el enlace del calendario del campus.")).toBeInTheDocument();
    expect(
      screen.getByText("En Google Calendar, desde la compu: Otros calendarios → + → Desde URL, y pegalo."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Listo: aparece en el celular, en la app de Google Calendar. Puede tardar unas horas en actualizarse.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("El calendario de Estudia se importa una vez: si cambia algo, exportalo de nuevo."),
    ).toBeInTheDocument();

    await userEvent.click(show);
    expect(fetchMock).toHaveBeenCalledWith("/api/calendar-feed/reveal", expect.objectContaining({ method: "POST" }));
    const field = await screen.findByRole("textbox", { name: "Enlace del calendario del campus" });
    expect(field).toHaveValue(FEED_URL);
    expect(field).toHaveAttribute("readonly");
    expect(field).toHaveAttribute("autocomplete", "off");
    expect(field).toHaveAttribute("spellcheck", "false");

    await userEvent.click(screen.getByRole("button", { name: "Copiar" }));
    expect(writeText).toHaveBeenCalledWith(FEED_URL);
    expect(await screen.findByText("Copiado")).toBeInTheDocument();

    // "Ocultar" lo saca de la pantalla.
    await userEvent.click(screen.getByRole("button", { name: "Ocultar" }));
    expect(screen.queryByDisplayValue(FEED_URL)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mostrar enlace" })).toBeInTheDocument();

    // No queda en ningún lado de la app: ni en el almacenamiento, ni en la dirección, ni en la consola.
    expect(JSON.stringify({ ...localStorage })).not.toContain("authtoken");
    expect(JSON.stringify({ ...sessionStorage })).not.toContain("authtoken");
    expect(window.location.href).not.toContain("authtoken");
    expect(log.mock.calls.flat().join(" ")).not.toContain("authtoken");

    // Al volver, hay que pedirlo de nuevo.
    await userEvent.click(screen.getByRole("button", { name: "Mostrar enlace" }));
    await screen.findByDisplayValue(FEED_URL);
    unmount();
    render(<Phone />);
    await screen.findByRole("button", { name: "Mostrar enlace" });
    expect(screen.queryByDisplayValue(FEED_URL)).not.toBeInTheDocument();
    log.mockRestore();
  });

  it("sin enlace del campus la guía explica dónde copiarlo", async () => {
    mockApi({ "GET /api/calendar-feed": { status: "unavailable", campus_calendar_url: CAMPUS_CALENDAR } });
    render(<Phone />);
    expect(
      await screen.findByText(
        "En el campus: Calendario → Importar o exportar calendarios → Exportar calendario → elegí «Todos los eventos» y «Eventos recientes y próximos» → Obtener URL del calendario.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /calendario del campus/i })).toHaveAttribute("href", CAMPUS_CALENDAR);
    expect(screen.queryByRole("button", { name: "Mostrar enlace" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Buscar el enlace" })).not.toBeInTheDocument();
  });

  it("sin saberlo todavía, primero busca el enlace", async () => {
    const fetchMock = mockApi({
      "GET /api/calendar-feed": { status: "unknown", campus_calendar_url: CAMPUS_CALENDAR },
      "POST /api/calendar-feed/fetch": { status: "saved", retry: false },
    });
    render(<Phone />);
    await userEvent.click(await screen.findByRole("button", { name: "Buscar el enlace" }));
    expect(fetchMock).toHaveBeenCalledWith("/api/calendar-feed/fetch", expect.objectContaining({ method: "POST" }));
    expect(await screen.findByRole("button", { name: "Mostrar enlace" })).toBeInTheDocument();
    // Encontrarlo no lo muestra: eso se pide aparte.
    expect(fetchMock).not.toHaveBeenCalledWith("/api/calendar-feed/reveal", expect.anything());
  });

  it("si el enlace ya no está guardado, lo dice y ofrece buscarlo", async () => {
    mockApi({
      "GET /api/calendar-feed": { status: "saved", campus_calendar_url: CAMPUS_CALENDAR },
      "POST /api/calendar-feed/reveal": () => jsonResponse(404, { detail: "No hay enlace guardado" }),
    });
    render(<Phone />);
    await userEvent.click(await screen.findByRole("button", { name: "Mostrar enlace" }));
    expect(await screen.findByText("El enlace ya no está guardado: buscalo de nuevo.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Buscar el enlace" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("si el campus no responde, lo dice con calma y deja lo que había", async () => {
    mockApi({
      "GET /api/calendar-feed": { status: "unknown", campus_calendar_url: CAMPUS_CALENDAR },
      "POST /api/calendar-feed/fetch": { status: "unknown", retry: true },
    });
    render(<Phone />);
    await userEvent.click(await screen.findByRole("button", { name: "Buscar el enlace" }));
    expect(await screen.findByText("No se pudo consultar el campus ahora. Probá más tarde.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Buscar el enlace" })).toBeEnabled());
  });

  it("sin preguntas esta semana, el mazo lo dice con calma", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    mockApi({
      "GET /api/calendar-feed": { status: "saved", campus_calendar_url: CAMPUS_CALENDAR },
      "GET /api/export/anki-semana.apkg": () => jsonResponse(404, { detail: "No hay preguntas para esta semana" }),
    });
    render(<Phone />);
    await userEvent.click(screen.getByRole("button", { name: "Bajá el mazo de la semana para el celular" }));
    expect(await screen.findByText("No hay preguntas para esta semana.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(click).not.toHaveBeenCalled();
    click.mockRestore();
  });

  it("si el mazo no se puede armar, pide probar de nuevo", async () => {
    mockApi({
      "GET /api/calendar-feed": { status: "saved", campus_calendar_url: CAMPUS_CALENDAR },
      "GET /api/export/anki-semana.apkg": () => jsonResponse(500, { detail: "error" }),
    });
    render(<Phone />);
    await userEvent.click(screen.getByRole("button", { name: "Bajá el mazo de la semana para el celular" }));
    expect(await screen.findByText("No se pudo armar el mazo. Probá de nuevo.")).toBeInTheDocument();
  });
});
