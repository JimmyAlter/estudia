import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Practice from "./Practice";
import type { Feedback, QuizItem } from "../api";
import { jsonResponse, mockApi } from "../test-utils";

const item = (over: Partial<QuizItem> = {}): QuizItem => ({
  question_id: 1,
  stem: "¿Cuál es la pendiente de y = 2x + 1?",
  options: ["1", "2", "3", "0"],
  topic: "Rectas",
  format: "mcq",
  phase: "focus",
  relearning: null,
  ...over,
});

const feedback = (over: Partial<Feedback> = {}): Feedback => ({
  correct: true,
  correct_index: 1,
  chosen_index: 1,
  explanation: "En y = mx + b la pendiente es m = 2.",
  common_error: "Tomar la ordenada al origen como pendiente.",
  source_seconds: null,
  youtube_id: null,
  typed_ok: null,
  requeue: false,
  next_label: "Vuelve en 3 días",
  relearn_progress: null,
  feedback_style: "full",
  ...over,
});

const answerBodies = (fetchMock: ReturnType<typeof mockApi>) =>
  fetchMock.mock.calls.filter(([url]) => url === "/api/answer").map(([, init]) => JSON.parse(String(init!.body)));

beforeEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    // sin almacenamiento: los avisos de una vez se muestran siempre
  }
});

afterEach(() => {
  vi.useRealTimers();
});

describe("Práctica v2", () => {
  it("Responder exige confianza y envía shown_order", async () => {
    const fetchMock = mockApi({
      "POST /api/quiz": [item()],
      "GET /api/materials": [],
      "POST /api/answer": feedback(),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);

    // Tocar una opción la marca y pide la confianza; todavía no responde.
    await userEvent.click(screen.getByRole("button", { name: /^2/ }));
    expect(answerBodies(fetchMock)).toEqual([]);
    expect(screen.queryByText(/Por qué es/)).not.toBeInTheDocument();
    for (const name of ["Seguro", "Me parece", "Adiviné"]) {
      expect(screen.getByRole("button", { name: new RegExp(`^${name}`) })).toBeInTheDocument();
    }

    await userEvent.click(screen.getByRole("button", { name: /^Me parece/ }));
    expect(await screen.findByText(/En y = mx \+ b/)).toBeInTheDocument();

    const [body] = answerBodies(fetchMock);
    // La posición tocada es dentro del orden mostrado; shown_order trae los índices originales.
    expect(body.shown_order).toHaveLength(4);
    expect([...body.shown_order].sort()).toEqual([0, 1, 2, 3]);
    expect(body.shown_order[body.chosen_index]).toBe(1);
    expect(body).toMatchObject({ question_id: 1, confidence: 1, mode: "mcq", typed_text: null });
    expect(typeof body.latency_ms).toBe("number");
  });

  it("los atajos 1 a 4 y S, M o A responden con el teclado", async () => {
    const fetchMock = mockApi({
      "POST /api/quiz": [item()],
      "GET /api/materials": [],
      "POST /api/answer": feedback(),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await userEvent.keyboard("3");
    await userEvent.keyboard("s");
    await screen.findByText(/En y = mx \+ b/);
    const [body] = answerBodies(fetchMock);
    expect(body.chosen_index).toBe(2);
    expect(body.confidence).toBe(2);
  });

  it("Pensalo primero oculta las opciones hasta tocar el botón", async () => {
    const fetchMock = mockApi({
      "POST /api/quiz": [item({ format: "think_first" })],
      "GET /api/materials": [],
      "POST /api/answer": feedback(),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);

    expect(screen.getByText("Pensá la respuesta antes de ver las opciones.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^2/ })).not.toBeInTheDocument();
    // Los números no eligen nada mientras las opciones están ocultas.
    await userEvent.keyboard("2");
    expect(screen.queryByRole("button", { name: /^Seguro/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Ver opciones" }));
    await userEvent.click(screen.getByRole("button", { name: /^2/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Seguro/ }));
    await screen.findByText(/En y = mx \+ b/);

    const [body] = answerBodies(fetchMock);
    expect(body.mode).toBe("mcq_think_first");
    expect(typeof body.options_revealed_ms).toBe("number");
  });

  it("Respuesta escrita corrige y ofrece opciones", async () => {
    let calls = 0;
    const fetchMock = mockApi({
      "POST /api/quiz": [item({ format: "typed" }), item({ question_id: 2, format: "typed", stem: "¿Cuánto es 3/4 + 1/4?" })],
      "GET /api/materials": [],
      "POST /api/answer": () =>
        ++calls === 1
          ? jsonResponse(400, { detail: "No pude leer la expresión" })
          : feedback({ chosen_index: null, typed_ok: true }),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);

    const field = screen.getByLabelText("Tu respuesta");
    expect(screen.getByText("Escribí el resultado: 2√2, 3/4, 2*sqrt(2)…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^2/ })).not.toBeInTheDocument();

    await userEvent.type(field, "2((");
    await userEvent.click(screen.getByRole("button", { name: "Corregir" }));
    // "Corregir" pasa a pedir la confianza.
    await userEvent.click(screen.getByRole("button", { name: /^Seguro/ }));

    // Ilegible: el error queda en el campo y no se gasta el intento.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("No pude leer la expresión");
    const again = screen.getByLabelText("Tu respuesta");
    expect(again).toHaveAttribute("aria-invalid", "true");
    expect(again).toHaveValue("2((");
    expect(screen.queryByText(/En y = mx \+ b/)).not.toBeInTheDocument();

    await userEvent.clear(again);
    await userEvent.type(again, "2");
    await userEvent.click(screen.getByRole("button", { name: "Corregir" }));
    await userEvent.click(screen.getByRole("button", { name: /^Seguro/ }));
    await screen.findByText(/En y = mx \+ b/);
    expect(screen.getByText(/Escribiste/)).toHaveTextContent("Escribiste 2: es correcta");
    // En una escrita, el título se lee natural.
    expect(screen.getByText("Respuesta correcta: 2")).toBeInTheDocument();
    expect(screen.queryByText(/Por qué es/)).not.toBeInTheDocument();
    const bodies = answerBodies(fetchMock);
    expect(bodies[1]).toMatchObject({ typed_text: "2", chosen_index: null, shown_order: [], mode: "typed", confidence: 2 });

    // La siguiente: "Mostrame opciones" pasa a typed_then_mcq.
    await userEvent.click(screen.getByRole("button", { name: /Siguiente/ }));
    await screen.findByText("¿Cuánto es 3/4 + 1/4?");
    await userEvent.click(screen.getByRole("button", { name: "Mostrame opciones" }));
    await userEvent.click(screen.getByRole("button", { name: /^2/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Adiviné/ }));
    await screen.findByText(/En y = mx \+ b/);
    expect(answerBodies(fetchMock)[2]).toMatchObject({ mode: "typed_then_mcq", typed_text: null, confidence: 0 });
    expect(answerBodies(fetchMock)[2].shown_order).toHaveLength(4);
  });

  it("Feedback alarm muestra el error común primero", async () => {
    mockApi({
      "POST /api/quiz": [item()],
      "GET /api/materials": [],
      "POST /api/answer": feedback({
        correct: false,
        chosen_index: 0,
        feedback_style: "alarm",
        source_seconds: 412,
        youtube_id: "abcdefghijk",
        next_label: "Vuelve al final de la sesión",
        relearn_progress: "0 de 3 días",
        requeue: true,
      }),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await userEvent.click(screen.getByRole("button", { name: /^1/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Seguro/ }));

    const headline = await screen.findByText("Estabas seguro y era otra. Fijate bien por qué.");
    const common = screen.getByText(/Tomar la ordenada al origen/);
    const explanation = screen.getByText(/En y = mx \+ b/);
    const minute = screen.getByRole("link", { name: /minuto 06:52/ });
    const before = (a: Node, b: Node) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
    expect(before(headline, common)).toBe(true);
    expect(before(common, explanation)).toBe(true);
    expect(before(explanation, minute)).toBe(true);

    // Reaprendizaje visible, con la explicación de una línea la primera vez.
    expect(screen.getByText("Vuelve al final de la sesión · 0 de 3 días")).toBeInTheDocument();
    expect(screen.getByText("La sabés cuando la acertás en tres días distintos.")).toBeInTheDocument();
    // Vuelve al final: la ronda crece.
    expect(screen.getByText(/pregunta 1 de 2/)).toBeInTheDocument();
  });

  it("acierto seguro dice Correcto con la explicación colapsada; dudando, abierta", async () => {
    let style: Feedback["feedback_style"] = "short";
    mockApi({
      "POST /api/quiz": [item(), item({ question_id: 2 })],
      "GET /api/materials": [],
      "POST /api/answer": () => feedback({ feedback_style: style }),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await userEvent.click(screen.getByRole("button", { name: /^2/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Seguro/ }));
    expect(await screen.findByText("Correcto")).toBeInTheDocument();
    expect(screen.getByText(/En y = mx \+ b/)).not.toBeVisible();

    style = "doubt";
    await userEvent.click(screen.getByRole("button", { name: "Siguiente" }));
    await userEvent.click(screen.getByRole("button", { name: /^2/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Adiviné/ }));
    expect(await screen.findByText("Bien, pero dudaste: leé por qué.")).toBeInTheDocument();
    expect(screen.getByText(/En y = mx \+ b/)).toBeVisible();
  });

  it("Enter no saltea el feedback antes de 1,5 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mockApi({
      "POST /api/quiz": [item(), item({ question_id: 2, stem: "Segunda pregunta" })],
      "GET /api/materials": [],
      "POST /api/answer": feedback(),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await user.keyboard("2");
    await user.keyboard("s");
    await screen.findByText(/En y = mx \+ b/);

    await user.keyboard("{Enter}");
    expect(screen.queryByText("Segunda pregunta")).not.toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(1000));
    await user.keyboard("{Enter}");
    expect(screen.queryByText("Segunda pregunta")).not.toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(600));
    await user.keyboard("{Enter}");
    expect(await screen.findByText("Segunda pregunta")).toBeInTheDocument();
  });

  it("Sesión mezclada muestra las dos fases", async () => {
    const fetchMock = mockApi({
      "POST /api/quiz": [
        item({ phase: "review", topic: "Derivadas", stem: "Repaso de derivadas" }),
        item({ question_id: 2, phase: "focus", topic: "Límites", stem: "Pregunta de límites" }),
      ],
      "GET /api/materials": [],
      "POST /api/answer": feedback(),
    });
    render(
      <Practice
        params={{ session_id: 7 }}
        session={{ kind: "mixed_focus", topic: "Límites", review_minutes: 10, focus_minutes: 40 }}
        onExit={() => {}}
      />,
    );
    await screen.findByText("Repaso de derivadas");
    const quizCall = fetchMock.mock.calls.find(([url]) => url === "/api/quiz");
    expect(JSON.parse(String(quizCall![1]!.body))).toEqual({ session_id: 7 });

    expect(screen.getByText("Repaso mezclado · 10 min")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Mezclamos temas a propósito: se siente más difícil, pero en el parcial vas a tener que reconocer qué método va.",
      ),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^2/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Seguro/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Siguiente" }));

    expect(await screen.findByText("Pregunta de límites")).toBeInTheDocument();
    expect(screen.getByText("Foco: Límites · 40 min")).toBeInTheDocument();
    expect(screen.queryByText("Repaso mezclado · 10 min")).not.toBeInTheDocument();
  });
});

describe("Práctica v2: ronda 1", () => {
  it("error dudando dice que no es correcta y marca la opción como incorrecta", async () => {
    mockApi({
      "POST /api/quiz": [item()],
      "GET /api/materials": [],
      "POST /api/answer": feedback({ correct: false, chosen_index: 0, feedback_style: "full" }),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await userEvent.click(screen.getByRole("button", { name: /^1/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Me parece/ }));
    expect(await screen.findByText("No es correcta")).toBeInTheDocument();
    expect(screen.getByText("Tu respuesta, incorrecta")).toBeInTheDocument();
  });

  it("una escrita mal respondida lo dice en la primera línea", async () => {
    mockApi({
      "POST /api/quiz": [item({ format: "typed" })],
      "GET /api/materials": [],
      "POST /api/answer": feedback({ correct: false, chosen_index: null, typed_ok: false, feedback_style: "full" }),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await userEvent.type(screen.getByLabelText("Tu respuesta"), "5");
    await userEvent.click(screen.getByRole("button", { name: "Corregir" }));
    await userEvent.click(screen.getByRole("button", { name: /^Me parece/ }));
    expect(await screen.findByText(/Escribiste/)).toHaveTextContent("Escribiste 5: no es correcta");
  });

  it("otro 400 se muestra tal cual, sin pedir que se escriba de otra forma", async () => {
    mockApi({
      "POST /api/quiz": [item({ format: "typed" })],
      "GET /api/materials": [],
      "POST /api/answer": () => jsonResponse(400, { detail: "Esta pregunta no admite respuesta escrita" }),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await userEvent.type(screen.getByLabelText("Tu respuesta"), "2");
    await userEvent.click(screen.getByRole("button", { name: "Corregir" }));
    await userEvent.click(screen.getByRole("button", { name: /^Seguro/ }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Esta pregunta no admite respuesta escrita");
    expect(alert).not.toHaveTextContent("Probá escribirlo de otra forma");
  });

  it("1 a 4 y S, M o A no hacen nada mientras se escribe la respuesta", async () => {
    const fetchMock = mockApi({
      "POST /api/quiz": [item({ format: "typed" })],
      "GET /api/materials": [],
      "POST /api/answer": feedback(),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    const field = screen.getByLabelText("Tu respuesta");
    await userEvent.type(field, "2sma");
    expect(field).toHaveValue("2sma");
    expect(screen.queryByRole("button", { name: /^Seguro/ })).not.toBeInTheDocument();
    expect(answerBodies(fetchMock)).toEqual([]);
  });

  it("\"Reportar un error\" va debajo de las opciones", async () => {
    mockApi({ "POST /api/quiz": [item()], "GET /api/materials": [] });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    const report = screen.getByRole("button", { name: "Reportar un error" });
    const lastOption = screen.getByRole("button", { name: /^0/ });
    expect(lastOption.compareDocumentPosition(report) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe("Práctica v2: ronda 2", () => {
  it("con la confianza a la vista, Corregir ya no está", async () => {
    mockApi({ "POST /api/quiz": [item({ format: "typed" })], "GET /api/materials": [] });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await userEvent.type(screen.getByLabelText("Tu respuesta"), "2");
    await userEvent.click(screen.getByRole("button", { name: "Corregir" }));
    expect(screen.getByRole("button", { name: /^Seguro/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Corregir" })).not.toBeInTheDocument();
    // Si cambia lo escrito, vuelve a pedir corregir.
    await userEvent.type(screen.getByLabelText("Tu respuesta"), "0");
    expect(screen.getByRole("button", { name: "Corregir" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Seguro/ })).not.toBeInTheDocument();
  });

  it("el contador no se corta", async () => {
    mockApi({ "POST /api/quiz": [item()], "GET /api/materials": [] });
    render(<Practice params={{}} onExit={() => {}} />);
    expect(await screen.findByText(/pregunta 1 de 1/)).toHaveClass("whitespace-nowrap");
  });
});

describe("Explicá por qué", () => {
  async function answerMeParece(fb: Feedback) {
    const fetchMock = mockApi({
      "POST /api/quiz": [item()],
      "GET /api/materials": [],
      "POST /api/answer": fb,
      "POST /api/answers/77/explain": { ok: true },
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await userEvent.click(screen.getByRole("button", { name: /^2/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Me parece/ }));
    return fetchMock;
  }
  const explainCalls = (fetchMock: ReturnType<typeof mockApi>) =>
    fetchMock.mock.calls.filter(([url]) => String(url).includes("/explain")).map(([url, init]) => [url, JSON.parse(String(init!.body))]);

  it("Explicá por qué aparece solo cuando la API lo pide y manda Sí o No", async () => {
    const fetchMock = await answerMeParece(feedback({ feedback_style: "doubt", answer_id: 77, explain_prompt: true }));
    const field = await screen.findByRole("textbox", { name: "Explicá por qué: escribilo en una línea" });
    // La explicación espera a que escriba o saltee.
    expect(screen.queryByText(/En y = mx \+ b/)).not.toBeInTheDocument();
    await userEvent.type(field, "porque m es el número que multiplica a x");
    await userEvent.click(screen.getByRole("button", { name: "Listo" }));

    expect(await screen.findByText(/En y = mx \+ b/)).toBeInTheDocument();
    expect(screen.getByText("porque m es el número que multiplica a x")).toBeInTheDocument();
    expect(screen.getByText("¿Dijiste lo mismo?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sí" }));
    await waitFor(() => expect(explainCalls(fetchMock)).toEqual([["/api/answers/77/explain", { same: true }]]));
    // Lo escrito no se manda.
    expect(JSON.stringify(explainCalls(fetchMock))).not.toContain("multiplica");
    expect(screen.queryByRole("button", { name: "Sí" })).not.toBeInTheDocument();
    expect(screen.getByText("Anotado. Cuenta para el repaso.")).toBeInTheDocument();
  });

  it("No manda same: false", async () => {
    const fetchMock = await answerMeParece(feedback({ feedback_style: "doubt", answer_id: 77, explain_prompt: true }));
    await userEvent.type(await screen.findByRole("textbox", { name: /Explicá por qué/ }), "ni idea");
    await userEvent.keyboard("{Enter}");
    await userEvent.click(await screen.findByRole("button", { name: "No" }));
    await waitFor(() => expect(explainCalls(fetchMock)).toEqual([["/api/answers/77/explain", { same: false }]]));
  });

  it("Saltear muestra la explicación sin llamar a nada", async () => {
    const fetchMock = await answerMeParece(feedback({ feedback_style: "doubt", answer_id: 77, explain_prompt: true }));
    await screen.findByRole("textbox", { name: /Explicá por qué/ });
    await userEvent.click(screen.getByRole("button", { name: "Saltear" }));
    expect(await screen.findByText(/En y = mx \+ b/)).toBeInTheDocument();
    expect(screen.queryByText("¿Dijiste lo mismo?")).not.toBeInTheDocument();
    expect(explainCalls(fetchMock)).toEqual([]);
  });

  it("sin explain_prompt no se pide", async () => {
    await answerMeParece(feedback({ feedback_style: "doubt", answer_id: 77, explain_prompt: false }));
    expect(await screen.findByText(/En y = mx \+ b/)).toBeInTheDocument();
    expect(screen.queryByText(/Explicá por qué/)).not.toBeInTheDocument();
  });
});

describe("Práctica: cita de un apunte", () => {
  it('la corrección cita "Apunte 3, pág. 14"', async () => {
    mockApi({
      "POST /api/quiz": [item()],
      "GET /api/materials": [],
      "POST /api/answer": feedback({
        correct: false,
        chosen_index: 0,
        source_page: 14,
        source_label: "Apunte 3, pág. 14",
        source_url: "https://campus.example/mod/resource/view.php?id=31",
      }),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await userEvent.click(screen.getByRole("button", { name: /^1/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Me parece/ }));

    const link = await screen.findByRole("link", { name: "Apunte 3, pág. 14" });
    expect(link).toHaveAttribute("href", "https://campus.example/mod/resource/view.php?id=31");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("sin enlace al campus, la cita queda como texto", async () => {
    mockApi({
      "POST /api/quiz": [item()],
      "GET /api/materials": [],
      "POST /api/answer": feedback({ correct: false, chosen_index: 0, source_page: 5, source_label: "Clase 2, diap. 5", source_url: null }),
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(/pendiente de y = 2x/);
    await userEvent.click(screen.getByRole("button", { name: /^1/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Me parece/ }));

    expect(await screen.findByText("Clase 2, diap. 5")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Clase 2/ })).not.toBeInTheDocument();
  });
});
