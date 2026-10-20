import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, onTestFinished, vi } from "vitest";
import Agenda from "./Agenda";
import type { AgendaWeek, ExamHealth, ExamInfo } from "../api";
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
      today: null, kind: "parcial", note: null, mock_suggested_at: null,
    },
  ],
};

/** El alta del examen (la Agenda también pide la lista de exámenes por GET). */
const postedExam = (fetchMock: ReturnType<typeof mockApi>) =>
  fetchMock.mock.calls.find(([url, init]) => url === "/api/exams" && init?.method === "POST");

describe("Agenda", () => {
  it("Agenda muestra los cinco tipos ordenados por hora", async () => {
    mockApi({ "GET /api/agenda": WEEK });
    const onPractice = vi.fn();
    render(<Agenda onPractice={onPractice} />);

    const day = await screen.findByRole("region", { name: /martes 13/i });
    // La clase se entiende sola; entrega, examen, evento y estudio llevan su rótulo chico.
    const kinds = within(day).getAllByTestId("agenda-kind").map((el) => el.textContent);
    expect(kinds).toEqual(["Entrega", "Examen", "Foco", "Evento"]);
    const titles = within(day).getAllByTestId("agenda-title").map((el) => el.textContent);
    expect(titles).toEqual(["Análisis Matemático", "TP 1", "Primer parcial", "Límites", "Consulta"]);
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

  it("el tipo de sesión va en el rótulo chico y el título dice solo los temas", async () => {
    const week = structuredClone(WEEK);
    const session = week.days[1].items.find((i) => i.kind === "session")!;
    week.days[1].items = [
      { ...session, id: 4, title: "Repaso mezclado: Límites, Derivadas y 1 más", session_kind: "mixed_only" },
      { ...session, id: 5, title: "Foco: Límites, con repaso de Derivadas", starts_at: at(21), ends_at: at(21, 50) },
    ];
    mockApi({ "GET /api/agenda": week });
    render(<Agenda onPractice={() => {}} />);
    const day = await screen.findByRole("region", { name: /martes 13/i });
    expect(within(day).getAllByTestId("agenda-kind").map((el) => el.textContent)).toEqual(["Repaso mezclado", "Foco"]);
    expect(within(day).getAllByTestId("agenda-title").map((el) => el.textContent)).toEqual([
      "Límites, Derivadas y 1 más",
      "Límites, con repaso de Derivadas",
    ]);
  });

  it("una sesión igual a la anterior del día dice que sigue el mismo repaso", async () => {
    const week = structuredClone(WEEK);
    const session = week.days[1].items.find((i) => i.kind === "session")!;
    const title = "Repaso mezclado: Límites y Derivadas";
    week.days[1].items = [
      { ...session, id: 4, title, session_kind: "mixed_only" },
      { ...session, id: 5, title, session_kind: "mixed_only", starts_at: at(20), ends_at: at(20, 50) },
    ];
    const onPractice = vi.fn();
    mockApi({ "GET /api/agenda": week });
    render(<Agenda onPractice={onPractice} />);
    const day = await screen.findByRole("region", { name: /martes 13/i });
    const titles = within(day).getAllByTestId("agenda-title");
    expect(titles.map((el) => el.textContent)).toEqual(["Límites y Derivadas", "Sigue el mismo repaso"]);
    expect(titles[1]).toHaveClass("text-muted");
    // Siguen siendo dos sesiones, cada una con su Practicar.
    const buttons = within(day).getAllByRole("button", { name: "Practicar" });
    expect(buttons).toHaveLength(2);
    await userEvent.click(buttons[1]);
    expect(onPractice).toHaveBeenCalledWith({ session_id: 5 }, expect.anything());
  });

  it("una sesión hecha se muestra como hecha y sin Practicar", async () => {
    const week = structuredClone(WEEK);
    week.days[1].items = week.days[1].items.map((i) => (i.kind === "session" ? { ...i, done: true } : i));
    mockApi({ "GET /api/agenda": week });
    render(<Agenda onPractice={() => {}} />);
    const day = await screen.findByRole("region", { name: /martes 13/i });
    expect(within(day).getByText("Hecha")).toHaveClass("sr-only");
    expect(within(day).getByText("Límites")).toHaveClass("line-through");
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

    const call = postedExam(fetchMock);
    expect(JSON.parse(String(call![1]!.body))).toEqual({
      course_id: 10,
      name: "Segundo parcial",
      at: Date.UTC(2026, 10, 20, 21) / 1000,
      max_grade: null,
      kind: "parcial",
    });
    await screen.findByRole("button", { name: "Agregar examen" });
    expect(loads).toBe(2);
  });

  it("Agenda muestra la inscripción con apertura y cierre", async () => {
    const week = structuredClone(WEEK);
    week.days[1].items = [
      { kind: "enrollment", phase: "open", id: 6, title: "Inscripción a finales", course: "Análisis Matemático", starts_at: at(9) },
    ];
    week.days[3].items = [
      { kind: "enrollment", phase: "close", id: 6, title: "Inscripción a finales", course: "Análisis Matemático", starts_at: Date.UTC(2026, 9, 15, 23 + 3, 59) / 1000 },
    ];
    mockApi({ "GET /api/agenda": week });
    render(<Agenda onPractice={() => {}} />);

    const tuesday = await screen.findByRole("region", { name: /martes 13/i });
    expect(within(tuesday).getByTestId("agenda-kind")).toHaveTextContent(/^Abre$/);
    expect(within(tuesday).getByTestId("agenda-title")).toHaveTextContent(/^Inscripción a finales$/);
    expect(within(tuesday).getByTestId("agenda-time")).toHaveTextContent("9:00");

    const thursday = screen.getByRole("region", { name: /jueves 15/i });
    expect(within(thursday).getByTestId("agenda-kind")).toHaveTextContent(/^Cierra$/);
    expect(within(thursday).getByTestId("agenda-title")).toHaveTextContent(/^Inscripción a finales$/);
    expect(within(thursday).getByTestId("agenda-time")).toHaveTextContent("23:59");
  });

  it("el mazo de la semana se baja desde Agenda", async () => {
    const apkg = new Uint8Array([80, 75, 3, 4]);
    mockApi({
      "GET /api/agenda": WEEK,
      "GET /api/export/anki-semana.apkg": () =>
        new Response(apkg, {
          status: 200,
          headers: { "Content-Disposition": 'attachment; filename="estudia-semana-2026-10-13.apkg"' },
        }),
    });
    const createObjectURL = vi.fn((_file: Blob) => "blob:mazo");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL }));
    const clicked: { href: string; download: string }[] = [];
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        clicked.push({ href: this.getAttribute("href") ?? "", download: this.download });
      });
    render(<Agenda onPractice={() => {}} />);
    const section = await screen.findByRole("region", { name: "En el celular" });
    expect(
      within(section).getByText("Se baja un archivo para abrir con Anki. Estudia no lo manda a ningún lado."),
    ).toBeInTheDocument();

    await userEvent.click(within(section).getByRole("button", { name: "Bajá el mazo de la semana para el celular" }));
    await waitFor(() => expect(clicked).toEqual([{ href: "blob:mazo", download: "estudia-semana-2026-10-13.apkg" }]));
    // Sin `expect.any(Blob)`: según la versión de Node, el Blob de `fetch` y el de jsdom son
    // clases distintas. Alcanza con que sea un archivo con tamaño y contenido legible.
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const file = createObjectURL.mock.calls[0][0];
    expect(typeof file.size).toBe("number");
    expect(typeof file.arrayBuffer).toBe("function");
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:mazo"));
    // El enlace temporal no queda en la página.
    expect(document.querySelector('a[href="blob:mazo"]')).toBeNull();
    expect(within(section).queryByRole("status")).not.toBeInTheDocument();
    click.mockRestore();
  });

  it("cargar una inscripción pide apertura y cierre", async () => {
    const fetchMock = mockApi({
      "GET /api/agenda": WEEK,
      "GET /api/courses": [{ id: 10, name: "Análisis Matemático" }],
      "POST /api/exams": { id: 11 },
    });
    render(<Agenda onPractice={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Agregar examen" }));
    const type = await screen.findByLabelText("Tipo");
    expect(within(type).getAllByRole("option").map((o) => o.textContent)).toContain("Inscripción");
    expect(screen.getByLabelText(/Nota máxima/)).toBeInTheDocument();

    await userEvent.selectOptions(type, "Inscripción");
    // Una inscripción no lleva nota: se piden apertura y cierre.
    expect(screen.queryByLabelText(/Nota máxima/)).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Nombre"), "Inscripción a finales");
    await userEvent.type(screen.getByLabelText("Abre"), "2026-11-02");
    await userEvent.clear(screen.getByLabelText("Hora de apertura"));
    await userEvent.type(screen.getByLabelText("Hora de apertura"), "09:00");

    // Sin cierre no se guarda.
    await userEvent.click(screen.getByRole("button", { name: "Guardar inscripción" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Elegí cuándo cierra.");
    expect(postedExam(fetchMock)).toBeUndefined();

    await userEvent.type(screen.getByLabelText("Cierra"), "2026-11-06");
    await userEvent.clear(screen.getByLabelText("Hora de cierre"));
    await userEvent.type(screen.getByLabelText("Hora de cierre"), "23:59");
    await userEvent.click(screen.getByRole("button", { name: "Guardar inscripción" }));

    await waitFor(() => {
      const call = postedExam(fetchMock);
      expect(JSON.parse(String(call![1]!.body))).toEqual({
        course_id: 10,
        name: "Inscripción a finales",
        at: Date.UTC(2026, 10, 2, 12) / 1000,
        closes_at: Date.UTC(2026, 10, 7, 2, 59) / 1000,
        max_grade: null,
        kind: "inscripcion",
      });
    });
  });

  it("una inscripción que cierra antes de abrir no se guarda", async () => {
    const fetchMock = mockApi({
      "GET /api/agenda": WEEK,
      "GET /api/courses": [{ id: 10, name: "Análisis Matemático" }],
    });
    render(<Agenda onPractice={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Agregar examen" }));
    await userEvent.selectOptions(await screen.findByLabelText("Tipo"), "Inscripción");
    await userEvent.type(screen.getByLabelText("Nombre"), "Inscripción a finales");
    await userEvent.type(screen.getByLabelText("Abre"), "2026-11-06");
    await userEvent.type(screen.getByLabelText("Cierra"), "2026-11-02");
    await userEvent.click(screen.getByRole("button", { name: "Guardar inscripción" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El cierre tiene que ser después de la apertura.");
    expect(postedExam(fetchMock)).toBeUndefined();
  });

  it("cargar un recuperatorio o un final desde Agenda", async () => {
    const fetchMock = mockApi({
      "GET /api/agenda": WEEK,
      "GET /api/courses": [{ id: 10, name: "Análisis Matemático" }],
      "POST /api/exams": { id: 12 },
    });
    render(<Agenda onPractice={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Agregar examen" }));
    const type = await screen.findByLabelText("Tipo");
    expect(within(type).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Parcial",
      "Recuperatorio",
      "Final",
      "Inscripción",
    ]);

    await userEvent.selectOptions(type, "Recuperatorio");
    expect(screen.getByText("Entran solo los temas que todavía no tenés firmes, sin contenido nuevo.")).toBeInTheDocument();
    expect(screen.queryByText(/Recursar es común/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Nota máxima/)).toBeInTheDocument();

    await userEvent.selectOptions(type, "Final");
    expect(screen.getByText("El plan arranca 6 semanas antes, con todos los temas de la materia.")).toBeInTheDocument();
    expect(screen.queryByText(/Entran solo los temas/)).not.toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Nombre"), "Final de Análisis");
    await userEvent.type(screen.getByLabelText("Día"), "2026-12-10");
    await userEvent.click(screen.getByRole("button", { name: "Guardar examen" }));
    await waitFor(() => {
      const call = postedExam(fetchMock);
      expect(JSON.parse(String(call![1]!.body))).toEqual({
        course_id: 10,
        name: "Final de Análisis",
        at: Date.UTC(2026, 11, 10, 12) / 1000,
        max_grade: null,
        kind: "final",
      });
    });
  });

  it("Armar el recuperatorio abre el alta con el tipo y la materia elegidos", async () => {
    const graded: ExamInfo = {
      id: 3, course_id: 20, course: "Física I", name: "Primer parcial", at: at(14), closes_at: null,
      source: "manual", kind: "parcial", topic_ids: [], max_grade: 10, time_limit_s: null, attempts: null,
      editable: true, result: { grade: 3, max_grade: 10, at: at(18) }, result_skipped: false,
    };
    mockApi({
      "GET /api/agenda": WEEK,
      "GET /api/exams": [graded],
      "GET /api/courses": [
        { id: 10, name: "Análisis Matemático" },
        { id: 20, name: "Física I" },
      ],
    });
    render(<Agenda onPractice={() => {}} />);
    const day = await screen.findByRole("region", { name: /martes 13/i });
    await userEvent.click(await within(day).findByRole("button", { name: "Armar el recuperatorio" }));

    const form = await screen.findByRole("form", { name: "Agregar examen" });
    expect(within(form).getByLabelText("Tipo")).toHaveValue("recuperatorio");
    await waitFor(() => expect(within(form).getByLabelText("Materia")).toHaveValue("20"));
    expect(within(form).getByText(/Entran solo los temas/)).toBeInTheDocument();
  });

  it("Armar el recuperatorio manda los temas del parcial de origen", async () => {
    const graded: ExamInfo = {
      id: 3, course_id: 20, course: "Física I", name: "Primer parcial", at: at(14), closes_at: null,
      source: "campus", kind: "parcial", topic_ids: [31, 32], max_grade: 10, time_limit_s: null, attempts: null,
      editable: false, result: { grade: 3, max_grade: 10, at: at(18) }, result_skipped: false,
    };
    const fetchMock = mockApi({
      "GET /api/agenda": WEEK,
      "GET /api/exams": [graded],
      "GET /api/courses": [
        { id: 10, name: "Análisis Matemático" },
        { id: 20, name: "Física I" },
      ],
      "POST /api/exams": { id: 12 },
    });
    render(<Agenda onPractice={() => {}} />);
    const day = await screen.findByRole("region", { name: /martes 13/i });
    await userEvent.click(await within(day).findByRole("button", { name: "Armar el recuperatorio" }));
    const form = await screen.findByRole("form", { name: "Agregar examen" });
    await waitFor(() => expect(within(form).getByLabelText("Materia")).toHaveValue("20"));
    await userEvent.type(within(form).getByLabelText("Nombre"), "Recuperatorio del primer parcial");
    await userEvent.type(within(form).getByLabelText("Día"), "2026-12-10");
    await userEvent.click(within(form).getByRole("button", { name: "Guardar examen" }));
    await waitFor(() => {
      const call = postedExam(fetchMock);
      expect(JSON.parse(String(call![1]!.body))).toEqual({
        course_id: 20,
        name: "Recuperatorio del primer parcial",
        at: Date.UTC(2026, 11, 10, 12) / 1000,
        max_grade: null,
        kind: "recuperatorio",
        topic_ids: [31, 32],
      });
    });
  });

  it("un examen sin nota no ofrece armar el recuperatorio", async () => {
    mockApi({
      "GET /api/agenda": WEEK,
      "GET /api/exams": [
        {
          id: 3, course_id: 20, course: "Física I", name: "Primer parcial", at: at(14), closes_at: null,
          source: "manual", kind: "parcial", topic_ids: [], max_grade: 10, time_limit_s: null, attempts: null,
          editable: true, result: null, result_skipped: false,
        },
      ],
    });
    render(<Agenda onPractice={() => {}} />);
    await screen.findByRole("region", { name: /martes 13/i });
    expect(screen.queryByRole("button", { name: "Armar el recuperatorio" })).not.toBeInTheDocument();
  });

  it("el recuperatorio sugiere un simulacro corto de 6 preguntas", async () => {
    const today = Math.floor(Date.now() / 1000);
    const retake: ExamHealth = {
      ...WEEK.health[0], exam_id: 7, exam: "Recuperatorio del primer parcial", verdict: "llegás",
      kind: "recuperatorio", note: null, mock_suggested_at: today,
    };
    mockApi({ "GET /api/agenda": { ...WEEK, health: [retake] } });
    const onShortMock = vi.fn();
    render(<Agenda onPractice={() => {}} onShortMock={onShortMock} />);
    // Con botón, solo el botón: el día ya va en él.
    await userEvent.click(await screen.findByRole("button", { name: "Hoy: simulacro corto" }));
    expect(onShortMock).toHaveBeenCalledWith(7);
    expect(screen.queryByText(/Simulacro corto sugerido/)).not.toBeInTheDocument();
  });

  it("sin botón, el simulacro corto se sugiere en una frase", async () => {
    // El reloj queda fijo el miércoles 14/10/2026, así el martes 20 nunca es hoy ni mañana.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(Date.UTC(2026, 9, 14, 15));
    onTestFinished(() => {
      vi.useRealTimers();
    });
    const tuesday = Date.UTC(2026, 9, 20, 15) / 1000;
    const retake: ExamHealth = {
      ...WEEK.health[0], exam_id: 7, exam: "Recuperatorio del primer parcial", verdict: "llegás",
      kind: "recuperatorio", note: null, mock_suggested_at: tuesday,
    };
    const today = Math.floor(Date.now() / 1000);
    mockApi({ "GET /api/agenda": { ...WEEK, health: [{ ...retake, exam_id: 8, mock_suggested_at: today }, retake] } });
    render(<Agenda onPractice={() => {}} />);
    expect(await screen.findByText("Simulacro corto sugerido para hoy.")).toBeInTheDocument();
    expect(screen.getByText("Simulacro corto sugerido para el martes 20.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /simulacro corto/ })).not.toBeInTheDocument();
  });

  it("el final con menos de 4 semanas lo dice en una línea", async () => {
    const final: ExamHealth = {
      ...WEEK.health[0], exam_id: 8, exam: "Final de Análisis", verdict: "justo", kind: "final",
      note: "Faltan menos de 4 semanas: el plan usa el tiempo que hay.", mock_suggested_at: null,
    };
    mockApi({ "GET /api/agenda": { ...WEEK, health: [final] } });
    render(<Agenda onPractice={() => {}} />);
    const note = await screen.findByText("Faltan menos de 4 semanas: el plan usa el tiempo que hay.");
    expect(note).toHaveClass("text-muted");
    expect(note.className).not.toMatch(/text-bad/);
    expect(screen.queryByText(/Simulacro corto sugerido/)).not.toBeInTheDocument();
  });
});
