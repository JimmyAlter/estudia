import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import KeyWizard, { KEY_FORMAT_TEXT, normalizeKey } from "./KeyWizard";
import { jsonResponse, mockApi } from "../test-utils";

const GOOD = "AIza" + "a".repeat(35);
const REJECTED = "Google no aceptó esa clave. Creá otra en Google AI Studio y pegala acá.";

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

describe("KeyWizard", () => {
  it("muestra los 3 pasos en texto y enlaza a Google AI Studio", () => {
    mockApi({});
    const { container } = render(<KeyWizard rejected={false} onDone={() => {}} />);
    expect(container.querySelectorAll("li")).toHaveLength(3);
    // El paso mismo es el enlace: no hay un segundo enlace al final del renglón.
    const link = screen.getByRole("link", { name: "Abrí Google AI Studio" });
    expect(link).toHaveClass("whitespace-nowrap");
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(link).toHaveAttribute("href", "https://aistudio.google.com/apikey");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(container.querySelector("img")).toBeNull();
    const input = screen.getByLabelText("Clave de Gemini");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");
    expect(input).toHaveAttribute("spellcheck", "false");
  });

  it("una clave con formato raro muestra la pista, no verifica sola y no bloquea el envío", async () => {
    const fetchMock = mockApi({ "POST /api/ai": () => jsonResponse(400, { detail: KEY_FORMAT_TEXT }) });
    render(<KeyWizard rejected={false} onDone={() => {}} />);
    const input = screen.getByLabelText("Clave de Gemini");
    await userEvent.click(input);
    await userEvent.paste("hola");
    expect(screen.getByText(KEY_FORMAT_TEXT)).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-describedby");
    expect(fetchMock).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Guardar clave" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]!.body));
    expect(body.key).toBe("hola");
  });

  it("al pegar una clave con formato válido se verifica sola", async () => {
    const onDone = vi.fn();
    const fetchMock = mockApi({ "POST /api/ai": { ok: true } });
    render(<KeyWizard rejected={false} onDone={onDone} />);
    await userEvent.click(screen.getByLabelText("Clave de Gemini"));
    await userEvent.paste(`  "${GOOD}"\n`);
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/ai");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init!.body))).toEqual({ provider: "gemini", key: GOOD });
  });

  it("el rechazo de Google aparece tal cual en el mismo campo y devuelve el foco", async () => {
    mockApi({ "POST /api/ai": () => jsonResponse(400, { detail: REJECTED }) });
    render(<KeyWizard rejected={false} onDone={() => {}} />);
    const input = screen.getByLabelText("Clave de Gemini");
    await userEvent.click(input);
    await userEvent.paste(GOOD);
    const message = await screen.findByText(REJECTED);
    expect(input).toHaveAttribute("aria-describedby", message.id);
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveFocus();
    // Lo que dijo Google va en tinta, sin rojo: no es una falta de quien la pegó.
    expect(message).toHaveRole("alert");
    expect(message).toHaveClass("text-ink");
    expect(message).not.toHaveClass("text-bad");
    expect(input.className).not.toMatch(/border-bad/);
  });

  it("vacío: Guardar clave no se puede tocar hasta que haya algo", async () => {
    const fetchMock = mockApi({});
    render(<KeyWizard rejected={false} onDone={() => {}} />);
    const save = screen.getByRole("button", { name: "Guardar clave" });
    expect(save).toBeDisabled();
    await userEvent.click(save);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    await userEvent.type(screen.getByLabelText("Clave de Gemini"), "a");
    expect(save).toBeEnabled();
    await userEvent.clear(screen.getByLabelText("Clave de Gemini"));
    expect(save).toBeDisabled();
  });

  it("un 503 se muestra como aviso aparte", async () => {
    mockApi({ "POST /api/ai": () => jsonResponse(503, { detail: "x" }) });
    render(<KeyWizard rejected={false} onDone={() => {}} />);
    await userEvent.type(screen.getByLabelText("Clave de Gemini"), "abc");
    await userEvent.click(screen.getByRole("button", { name: "Guardar clave" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo verificar la clave ahora. Probá en un rato.");
  });

  it("Mostrar aparece con algo escrito y alterna la visibilidad", async () => {
    mockApi({});
    render(<KeyWizard rejected={false} onDone={() => {}} />);
    expect(screen.queryByRole("button", { name: "Mostrar" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Clave de Gemini"), "abc");
    await userEvent.click(screen.getByRole("button", { name: "Mostrar" }));
    expect(screen.getByLabelText("Clave de Gemini")).toHaveAttribute("type", "text");
  });

  it("la clave no queda en la URL ni en el almacenamiento", async () => {
    let calls = 0;
    const fetchMock = mockApi({
      "POST /api/ai": () => (++calls === 1 ? jsonResponse(400, { detail: REJECTED }) : jsonResponse(200, { ok: true })),
    });
    const onDone = vi.fn();
    render(<KeyWizard rejected={false} onDone={onDone} />);
    const input = screen.getByLabelText("Clave de Gemini");
    await userEvent.click(input);
    await userEvent.paste(GOOD);
    await screen.findByText(REJECTED);
    await userEvent.click(screen.getByRole("button", { name: "Guardar clave" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(input).toHaveValue("");
    expect(window.location.href).not.toContain(GOOD);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    for (const [url] of fetchMock.mock.calls) expect(String(url)).not.toContain(GOOD);
  });
});

describe("normalizeKey", () => {
  it("saca espacios y comillas de los bordes; null si queda vacía", () => {
    expect(normalizeKey(`  “${GOOD}”\n`)).toBe(GOOD);
    expect(normalizeKey(`'${GOOD}`)).toBe(GOOD);
    expect(normalizeKey(' "" ')).toBeNull();
    expect(normalizeKey("")).toBeNull();
  });
});
