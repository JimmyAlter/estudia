import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Agenda from "./Agenda";
import type { AgendaWeek } from "../api";
import { mockApi } from "../test-utils";

// Martes 13/10/2026, en hora de Buenos Aires (UTC−3).
const at = (h: number, m = 0) => Date.UTC(2026, 9, 13, h + 3, m) / 1000;

const WEEK: AgendaWeek = {
  days: [
    { date: "2026-10-12", items: [] },
    {
      date: "2026-10-13",
      items: [
        { kind: "class", id: 1, title: "Análisis Matemático", course: "Análisis Matemático", starts_at: at(8, 30), ends_at: at(10, 30), place: "Aula 204" },
        { kind: "deadline", id: 2, title: "TP 1", course: "Análisis Matemático", starts_at: at(12), url: "https://campus.example/tp1" },
        { kind: "exam", id: 3, title: "Primer parcial", course: "Análisis Matemático", starts_at: at(14), ends_at: at(16) },
        {
          kind: "session", id: 4, title: "Foco: Límites", course: "Análisis Matemático", starts_at: at(19), ends_at: at(19, 50),
          topic_id: 7, topic: "Límites", done: false, session_kind: "mixed_focus", topic_ids: [7, 8], exam_id: 3,
          review_minutes: 10, focus_minutes: 40,
        },
        { kind: "event", id: 5, title: "Consulta", course: "Análisis Matemático", starts_at: at(21), url: null },
      ],
    },
    { date: "2026-10-14", items: [] },
    { date: "2026-10-15", items: [] },
    { date: "2026-10-16", items: [] },
    { date: "2026-10-17", items: [] },
    { date: "2026-10-18", items: [] },
  ],
  health: [
    {
      exam_id: 3, exam: "Primer parcial", course: "Análisis Matemático", at: at(14), minutes_needed: 360,
      minutes_available: 240, verdict: "no llegás", due_reviews: 3,
      weak_topics: [
        { topic_id: 7, topic: "Límites", probability: 0.2, minutes: 200 },
        { topic_id: 8, topic: "Derivadas", probability: 0.4, minutes: 160 },
      ],
      today: null,
    },
  ],
};

describe("Agenda", () => {
  it("Agenda muestra los cinco tipos ordenados por hora", async () => {
    mockApi({ "GET /api/agenda": WEEK });
    const onPractice = vi.fn();
    render(<Agenda onPractice={onPractice} />);

    const day = await screen.findByRole("region", { name: /martes 13/i });
    // Clase y estudio se entienden solos; entrega, examen y evento llevan su rótulo chico.
    const kinds = within(day).getAllByTestId("agenda-kind").map((el) => el.textContent);
    expect(kinds).toEqual(["Entrega", "Examen", "Evento"]);
    const titles = within(day).getAllByTestId("agenda-title").map((el) => el.textContent);
    expect(titles).toEqual(["Análisis Matemático", "TP 1", "Primer parcial", "Foco: Límites", "Consulta"]);
    // Con una sola materia no se repite en cada ítem.
    expect(within(day).getAllByText("Análisis Matemático")).toHaveLength(1);
    const times = within(day).getAllByTestId("agenda-time").map((el) => el.textContent);
    expect(times).toEqual(["8:30", "12:00", "14:00", "19:00", "21:00"]);
    expect(within(day).getByText(/Aula 204/)).toBeInTheDocument();
    expect(within(day).getByRole("link", { name: "Abrir en el campus" })).toHaveAttribute("href", "https://campus.example/tp1");

    // ¿Llegás? en una línea, con el porqué.
    expect(screen.getByText(/Primer parcial: con tu tiempo actual no llegás/)).toBeInTheDocument();
    expect(screen.getByText(/Te faltan Límites y Derivadas, unas 6 h, y tenés 4 h libres/)).toBeInTheDocument();

    await userEvent.click(within(day).getByRole("button", { name: "Practicar" }));
    expect(onPractice).toHaveBeenCalledWith(
      { session_id: 4 },
      { kind: "mixed_focus", topic: "Límites", review_minutes: 10, focus_minutes: 40 },
    );
  });

  it("una sesión hecha se muestra como hecha y sin Practicar", async () => {
    const week = structuredClone(WEEK);
    week.days[1].items = week.days[1].items.map((i) => (i.kind === "session" ? { ...i, done: true } : i));
    mockApi({ "GET /api/agenda": week });
    render(<Agenda onPractice={() => {}} />);
    const day = await screen.findByRole("region", { name: /martes 13/i });
    expect(within(day).getByText("Hecha")).toHaveClass("sr-only");
    expect(within(day).getByText("Foco: Límites")).toHaveClass("line-through");
    expect(within(day).queryByRole("button", { name: "Practicar" })).not.toBeInTheDocument();
  });

  it("Exportar descarga el ics", async () => {
    mockApi({ "GET /api/agenda": WEEK });
    render(<Agenda onPractice={() => {}} />);
    const link = await screen.findByRole("link", { name: "Exportar calendario" });
    expect(link).toHaveAttribute("href", "/api/calendar.ics");
    expect(link).toHaveAttribute("download");
  });

  it("Agregar examen lo crea y recarga la semana", async () => {
    let loads = 0;
    const fetchMock = mockApi({
      "GET /api/agenda": () => (loads++, WEEK),
      "GET /api/courses": [{ id: 10, name: "Análisis Matemático" }],
      "POST /api/exams": { id: 9 },
    });
    render(<Agenda onPractice={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Agregar examen" }));
    await userEvent.type(await screen.findByLabelText("Nombre"), "Segundo parcial");
    await userEvent.type(screen.getByLabelText("Día"), "2026-11-20");
    await userEvent.clear(screen.getByLabelText("Hora"));
    await userEvent.type(screen.getByLabelText("Hora"), "18:00");
    await userEvent.click(screen.getByRole("button", { name: "Guardar examen" }));

    const call = fetchMock.mock.calls.find(([url]) => url === "/api/exams");
    expect(JSON.parse(String(call![1]!.body))).toEqual({
      course_id: 10,
      name: "Segundo parcial",
      at: Date.UTC(2026, 10, 20, 21) / 1000,
      max_grade: null,
    });
    await screen.findByRole("button", { name: "Agregar examen" });
    expect(loads).toBe(2);
  });
});
