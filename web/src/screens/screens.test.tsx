import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Home from "./Home";
import Materials from "./Materials";
import Onboarding from "./Onboarding";
import Practice from "./Practice";
import type { AppState } from "../api";
import { jsonResponse, mockApi } from "../test-utils";

const QUIZ = [
  { question_id: 1, stem: "La recta de pendiente 3 que pasa por (2, 1) corta al eje y en…", options: ["1", "7", "−5", "3"], topic: "Funciones lineales" },
  { question_id: 2, stem: "¿Cuál es la pendiente de y = 2x + 1?", options: ["1", "2", "3", "0"], topic: "Funciones lineales" },
];

const FEEDBACK = {
  correct: false,
  correct_index: 2,
  explanation: "y = 3x + b. Como (2, 1) está en la recta: 1 = 3·2 + b, entonces b = −5.",
  common_error: "Sumar en vez de restar al despejar b.",
  source_seconds: 1060,
  youtube_id: "abcdefghijk",
};

describe("Practice", () => {
  it("muestra la explicación recién después de responder", async () => {
    const fetchMock = mockApi({
      "POST /api/quiz": QUIZ,
      "GET /api/materials": [],
      "POST /api/answer": FEEDBACK,
    });
    render(<Practice params={{}} onExit={() => {}} />);

    await screen.findByText(QUIZ[0].stem);
    expect(screen.queryByText(/Por qué es/)).not.toBeInTheDocument();
    expect(screen.queryByText(FEEDBACK.explanation)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^7/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Me parece/ }));

    expect(await screen.findByText("Por qué es −5")).toBeInTheDocument();
    // Las opciones se muestran mezcladas: se manda la posición tocada y el orden mostrado.
    const answerCall = fetchMock.mock.calls.find(([url]) => url === "/api/answer");
    const body = JSON.parse(String(answerCall![1]!.body));
    expect(body.shown_order[body.chosen_index]).toBe(1);
    expect(screen.getByText(FEEDBACK.explanation)).toBeInTheDocument();
    expect(screen.getByText(/Sumar en vez de restar/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /minuto 17:40/ });
    expect(link).toHaveAttribute("href", "https://youtu.be/abcdefghijk?t=1060");
  });

  it("el texto de la opción puede achicarse (no ensancha el botón)", async () => {
    mockApi({ "POST /api/quiz": QUIZ, "GET /api/materials": [] });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(QUIZ[0].stem);
    const option = screen.getByRole("button", { name: /^7/ });
    expect(option.querySelector(".math")).toHaveClass("min-w-0", "flex-1");
  });

  it("\"Reportar un error\" la retira y pasa a la siguiente", async () => {
    const fetchMock = mockApi({
      "POST /api/quiz": QUIZ,
      "GET /api/materials": [],
      "POST /api/question/1/retire": { ok: true },
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(QUIZ[0].stem);

    await userEvent.click(screen.getByRole("button", { name: "Reportar un error" }));

    expect(await screen.findByText(QUIZ[1].stem)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/question/1/retire",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("Practice: deshacer el reporte", () => {
  it("\"Deshacer\" llama a unretire y la pregunta vuelve", async () => {
    const fetchMock = mockApi({
      "POST /api/quiz": QUIZ,
      "GET /api/materials": [],
      "POST /api/question/1/retire": { ok: true },
      "POST /api/question/1/unretire": { ok: true },
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(QUIZ[0].stem);

    await userEvent.click(screen.getByRole("button", { name: "Reportar un error" }));
    expect(await screen.findByText(/Listo, no te la vamos a mostrar más/)).toBeInTheDocument();
    expect(screen.getByText(/pregunta 1 de 1/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Deshacer" }));
    expect(await screen.findByText(QUIZ[0].stem)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/question/1/unretire",
      expect.objectContaining({ method: "POST" }),
    );
    expect(screen.getByText(/pregunta 1 de 2/)).toBeInTheDocument();
  });
});

describe("Home", () => {
  it("sin examen muestra \"Sin exámenes próximos\"", async () => {
    mockApi({
      "GET /api/home": {
        exam: null,
        topics: [],
        suggestion: null,
        processing: { pending: 0, done: 0, failed: 0 },
      },
    });
    render(<Home refreshKey={0} aiStopReason={null} working={false} onPractice={() => {}} onNavigate={() => {}} onSessionExpired={() => {}} />);
    expect(await screen.findByText("Sin exámenes próximos")).toBeInTheDocument();
    expect(screen.queryByText("Próximo examen")).not.toBeInTheDocument();
  });

  it("dice cuántas clases se preparan y sugiere lo que nunca se practicó", async () => {
    const topic = { id: 3, name: "Funciones cuadráticas", mastery: 0, practiced: false, seen: 0, total: 4, at: null };
    mockApi({
      "GET /api/home": {
        exam: null,
        topics: [topic],
        suggestion: topic,
        processing: { pending: 1, done: 2, failed: 0 },
      },
    });
    const { unmount } = render(
      <Home refreshKey={0} aiStopReason={null} working onPractice={() => {}} onNavigate={() => {}} onSessionExpired={() => {}} />,
    );
    expect(await screen.findByText("Preparando 1 clase")).toBeInTheDocument();
    expect(screen.getByText(/Todavía no practicaste:/)).toBeInTheDocument();
    unmount();

    // Sin nada corriendo no se promete avance.
    render(<Home refreshKey={0} aiStopReason={null} working={false} onPractice={() => {}} onNavigate={() => {}} onSessionExpired={() => {}} />);
    expect(await screen.findByText("1 clase por preparar")).toBeInTheDocument();
  });
});

describe("Home: barra de probabilidad", () => {
  it("la barra llega justo al porcentaje que se muestra", async () => {
    const topic = { id: 1, name: "Rectas", mastery: 0.666, practiced: true, seen: 8, total: 8, at: null };
    mockApi({
      "GET /api/home": {
        exam: null,
        topics: [topic],
        suggestion: null,
        processing: { pending: 0, done: 1, failed: 0 },
      },
    });
    render(<Home refreshKey={0} aiStopReason={null} working={false} onPractice={() => {}} onNavigate={() => {}} onSessionExpired={() => {}} />);
    expect(await screen.findByText("Probabilidad de que lo sepas hoy")).toBeInTheDocument();
    expect(screen.getByText("67 %")).toBeInTheDocument();
    expect(screen.getByTestId("mastery-bar")).toHaveStyle({ width: "67%" });
  });
});

describe("Onboarding", () => {
  it("muestra el aviso de Gemini en el paso 3", async () => {
    mockApi({});
    const state: AppState = {
      step: "ia",
      site_id: "utn-frba",
      ai_stop_reason: null,
      syncing: false,
      last_sync_at: null,
      last_sync_error: null,
    };
    render(<Onboarding state={state} onChange={() => {}} />);
    await waitFor(() =>
      expect(
        screen.getByText(/En el plan gratuito, personas de Google pueden leer lo que se envía/),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText("Qué ve Google")).toBeInTheDocument();
    expect(screen.getByText(/Google recibe el video de cada clase/)).toBeInTheDocument();
    expect(screen.getByText(/ni tus datos personales/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /clave/i })).toHaveAttribute(
      "href",
      "https://aistudio.google.com/apikey",
    );
  });
});

describe("Onboarding: puerta de edad", () => {
  const base: AppState = {
    step: "edad", age: null, site_id: "utn-frba", ai_stop_reason: null, syncing: false,
    last_sync_at: null, last_sync_error: null,
  };

  it("pregunta la edad antes de la clave, en el mismo paso 3 de 3", () => {
    mockApi({});
    render(<Onboarding state={base} onChange={() => {}} />);
    expect(screen.getByText("Paso 3 de 3")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "¿Tenés 18 años o más?" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Clave de Gemini")).not.toBeInTheDocument();
  });

  it("menor no ve el paso de la clave", async () => {
    const onChange = vi.fn();
    const fetchMock = mockApi({ "PUT /api/settings": { ok: true } });
    render(<Onboarding state={base} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: "No" }));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    const put = fetchMock.mock.calls.find(([u, init]) => u === "/api/settings" && init?.method === "PUT")!;
    expect(JSON.parse(String(put[1]!.body))).toEqual({ age: "minor" });
    expect(screen.queryByLabelText("Clave de Gemini")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith("/api/ai", expect.anything());
  });

  it("adulto sigue al paso de la clave", async () => {
    const onChange = vi.fn();
    const fetchMock = mockApi({ "PUT /api/settings": { ok: true } });
    render(<Onboarding state={base} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: "Sí" }));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    const put = fetchMock.mock.calls.find(([u, init]) => u === "/api/settings" && init?.method === "PUT")!;
    expect(JSON.parse(String(put[1]!.body))).toEqual({ age: "adult" });
  });
});

describe("Onboarding: clave vacía", () => {
  it("\"Guardar clave\" avisa si falta la clave y no la manda", async () => {
    const fetchMock = mockApi({});
    const state: AppState = {
      step: "ia", site_id: "utn-frba", ai_stop_reason: null, syncing: false,
      last_sync_at: null, last_sync_error: null,
    };
    render(<Onboarding state={state} onChange={() => {}} />);
    const save = screen.getByRole("button", { name: "Guardar clave" });
    expect(save).toBeEnabled();
    await userEvent.click(save);
    const message = screen.getByRole("alert");
    expect(message).toHaveTextContent("Pegá la clave primero.");
    const input = screen.getByLabelText("Clave de Gemini");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAttribute("aria-describedby", message.id);
    expect(input).toHaveFocus();
    // El mensaje va justo debajo del campo, antes del botón.
    expect(input.nextElementSibling).toBe(message);
    expect(fetchMock).not.toHaveBeenCalledWith("/api/ai", expect.anything());
  });
});

describe("Onboarding: el aviso de la clave", () => {
  it("se borra cuando se escribe en el campo", async () => {
    mockApi({ "POST /api/ai": () => jsonResponse(400, { detail: "la clave fue rechazada" }) });
    const state: AppState = {
      step: "ia", site_id: "utn-frba", ai_stop_reason: null, syncing: false,
      last_sync_at: null, last_sync_error: null,
    };
    render(<Onboarding state={state} onChange={() => {}} />);
    const input = screen.getByLabelText("Clave de Gemini");
    await userEvent.type(input, "clave-mala");
    await userEvent.click(screen.getByRole("button", { name: "Guardar clave" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Google rechazó esa clave");

    await userEvent.type(input, "x");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(input).not.toHaveAttribute("aria-invalid");
  });
});

describe("Sin IA: textos de preparación", () => {
  const HOME = {
    exam: null, topics: [], suggestion: null, processing: { pending: 3, done: 1, failed: 1 },
  };
  const homeProps = { refreshKey: 0, aiStopReason: null, working: false, onPractice: () => {}, onNavigate: () => {}, onSessionExpired: () => {} };

  it("menor: Inicio no promete clases por preparar y lo dice una vez", async () => {
    mockApi({ "GET /api/home": HOME });
    render(<Home {...homeProps} age="minor" />);
    expect(await screen.findByText("Sin IA no se preparan clases nuevas. Podés practicar con las preguntas que ya hay.")).toBeInTheDocument();
    expect(screen.queryByText(/por preparar|Preparando/)).not.toBeInTheDocument();
    expect(screen.queryByText(/no se pudo procesar|no se pudieron procesar/)).not.toBeInTheDocument();
  });

  it("sin responder: Inicio oculta la línea de por preparar", async () => {
    mockApi({ "GET /api/home": HOME });
    render(<Home {...homeProps} age={null} />);
    await waitFor(() => expect(screen.getByText("Ver materiales")).toBeInTheDocument());
    expect(screen.queryByText(/por preparar|Preparando/)).not.toBeInTheDocument();
  });

  it("adulto: sigue diciendo cuántas faltan", async () => {
    mockApi({ "GET /api/home": HOME });
    render(<Home {...homeProps} age="adult" />);
    expect(await screen.findByText("3 clases por preparar")).toBeInTheDocument();
  });

  const SECTIONS = [{
    id: 1, course_id: 1, num: 1, name: "S1", course: "C", materials: [
      { id: 1, title: "Clase 1", kind: "youtube", status: "pending", summary_md: null, error: null, youtube_id: "a", campus_url: null, topic_ids: [], questions: 0 },
    ],
  }];

  it.each(["minor", null] as const)("Materiales dice Sin preparar con edad %s", async (age) => {
    mockApi({ "GET /api/materials": SECTIONS });
    render(<Materials aiStopReason={null} age={age} onPractice={() => {}} />);
    expect(await screen.findByText("Sin preparar")).toBeInTheDocument();
    expect(screen.queryByText("En preparación")).not.toBeInTheDocument();
  });

  it("Materiales de un adulto dice En preparación", async () => {
    mockApi({ "GET /api/materials": SECTIONS });
    render(<Materials aiStopReason={null} age="adult" onPractice={() => {}} />);
    expect(await screen.findByText("En preparación")).toBeInTheDocument();
  });
});

describe("Materials", () => {
  it("muestra el estado solo si la clase no está lista y enlaza al campus lo que no se puede preparar", async () => {
    const base = { kind: "youtube", summary_md: null, error: null, youtube_id: null, campus_url: null };
    mockApi({
      "GET /api/materials": [
        {
          id: 1, course_id: 1, course: "Matemática", num: 1, name: "UNIDAD 1",
          materials: [
            { ...base, id: 1, title: "Video-clase 1: Rectas.", status: "done", summary_md: "# Rectas\nTexto." },
            { ...base, id: 2, title: "Video-clase 3", status: "pending" },
            { ...base, id: 3, title: "Guía", kind: "page", status: "unsupported",
              campus_url: "https://campus.example/mod/page/view.php?id=9" },
          ],
        },
      ],
    });
    render(<Materials aiStopReason="quota" />);
    expect(await screen.findByText("Video-clase 1: Rectas")).toBeInTheDocument();
    expect(screen.queryByText("listo")).not.toBeInTheDocument();
    expect(screen.getByText("Se prepara mañana")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir en el campus" })).toHaveAttribute(
      "href",
      "https://campus.example/mod/page/view.php?id=9",
    );
  });
});

describe("Materials: reintentar", () => {
  it("\"Reintentar las que fallaron\" vuelve a poner en cola y recarga", async () => {
    const base = { kind: "youtube", summary_md: null, error: null, youtube_id: null, campus_url: null };
    let status = "failed";
    const fetchMock = mockApi({
      "GET /api/materials": () => [
        { id: 1, course_id: 1, course: "Matemática", num: 1, name: "UNIDAD 1",
          materials: [{ ...base, id: 1, title: "Clase 2", status }] },
      ],
      "POST /api/materials/retry-failed": () => {
        status = "pending";
        return { reset: 1 };
      },
    });
    render(<Materials aiStopReason={null} />);
    expect(await screen.findByText("No se pudo preparar")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Reintentar las que fallaron" }));

    expect(await screen.findByText("En preparación")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reintentar las que fallaron" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/materials/retry-failed",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("sin clases fallidas no ofrece reintentar", async () => {
    const base = { kind: "youtube", summary_md: null, error: null, youtube_id: null, campus_url: null };
    mockApi({
      "GET /api/materials": [
        { id: 1, course_id: 1, course: "Matemática", num: 1, name: "UNIDAD 1",
          materials: [{ ...base, id: 1, title: "Clase 2", status: "pending" }] },
      ],
    });
    render(<Materials aiStopReason={null} />);
    expect(await screen.findByText("Clase 2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reintentar las que fallaron" })).not.toBeInTheDocument();
  });
});

describe("Materials: video que no es público", () => {
  const COPY = "Este video no es público. Pedile a la cátedra el archivo en el campus, o activá la descarga en Ajustes";

  it("muestra el texto apagado con un enlace a Ajustes y ofrece reintentar", async () => {
    const base = { kind: "youtube", summary_md: null, youtube_id: "a", campus_url: null, topic_ids: [], questions: 0 };
    mockApi({
      "GET /api/materials": [
        { id: 1, course_id: 1, course: "Matemática", num: 1, name: "UNIDAD 1",
          materials: [{ ...base, id: 1, title: "Clase 4", status: "needs_public_link", error: COPY }] },
      ],
    });
    render(<Materials aiStopReason={null} age="adult" />);
    const title = await screen.findByText("Clase 4");
    expect(title).toHaveClass("text-muted");
    const note = screen.getByText((_, el) => el?.tagName === "P" && el.textContent === COPY);
    expect(note).toHaveClass("text-muted");
    expect(screen.getByRole("link", { name: "Ajustes" })).toHaveAttribute("href", "#/ajustes");
    expect(screen.queryByText("No se pudo preparar")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reintentar las que fallaron" })).toBeInTheDocument();
  });
});

describe("Materials: tope de YouTube y sin IA", () => {
  const base = { kind: "youtube", summary_md: null, error: null, youtube_id: "a", campus_url: null, topic_ids: [], questions: 0 };
  const sections = (status: string, error: string | null = null) => [
    { id: 1, course_id: 1, course: "C", num: 1, name: "S1", materials: [{ ...base, id: 1, title: "Clase 5", status, error }] },
  ];

  it("con el tope de YouTube lo pendiente dice Se prepara mañana", async () => {
    mockApi({ "GET /api/materials": sections("pending") });
    render(<Materials aiStopReason="youtube_cap" age="adult" />);
    expect(await screen.findByText("Se prepara mañana")).toBeInTheDocument();
  });

  it.each(["minor", null] as const)("sin IA (edad %s) un video sin enlace público dice Sin preparar", async (age) => {
    mockApi({ "GET /api/materials": sections("needs_public_link", "Este video no es público.") });
    render(<Materials aiStopReason={null} age={age} />);
    expect(await screen.findByText("Sin preparar")).toBeInTheDocument();
    expect(screen.queryByText(/Este video no es público/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Ajustes" })).not.toBeInTheDocument();
  });
});

describe("Onboarding: pegar el enlace del campus", () => {
  const loginState: AppState = {
    step: "login", site_id: "utn-frba", ai_stop_reason: null, syncing: false,
    last_sync_at: null, last_sync_error: null,
  };
  const PASTED = "moodlemobile://token=c2VjcmV0bw==";

  it("manda el enlace pegado a /api/login/callback y sigue", async () => {
    const fetchMock = mockApi({
      "POST /api/login/start": { url: "https://campus.example/launch" },
      "POST /api/login/callback": { ok: true },
    });
    let done = 0;
    render(<Onboarding state={loginState} onChange={() => done++} />);
    expect(screen.queryByLabelText(/No volvió solo/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Iniciar sesión en el campus" }));

    const field = await screen.findByLabelText(
      "¿No volvió solo? Pegá acá el enlace que empieza con moodlemobile:// o estudia://",
    );
    await userEvent.type(field, PASTED);
    await userEvent.click(screen.getByRole("button", { name: "Continuar" }));

    await waitFor(() => expect(done).toBe(1));
    const call = fetchMock.mock.calls.find(([url]) => url === "/api/login/callback");
    expect(JSON.parse(String(call![1]!.body))).toEqual({ url: PASTED });
  });

  it("si el enlace no sirve avisa sin repetirlo", async () => {
    mockApi({
      "POST /api/login/start": { url: "https://campus.example/launch" },
      "POST /api/login/callback": () => jsonResponse(400, { detail: "respuesta de inicio de sesión inválida" }),
    });
    render(<Onboarding state={loginState} onChange={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Iniciar sesión en el campus" }));
    const field = await screen.findByLabelText(/No volvió solo/);
    await userEvent.type(field, PASTED);
    await userEvent.click(screen.getByRole("button", { name: "Continuar" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Ese enlace no sirvió");
    expect(alert.textContent).not.toContain("c2VjcmV0bw");
    expect(field).toHaveAttribute("aria-invalid", "true");
  });
});

describe("Materials: exportar a Anki", () => {
  it("enlaza a la descarga general y a la de cada curso si hay más de uno", async () => {
    const base = { kind: "youtube", summary_md: null, error: null, youtube_id: null, campus_url: null, status: "done" };
    mockApi({
      "GET /api/materials": [
        { id: 1, course_id: 10, course: "Matemática", num: 1, name: "U1", materials: [{ ...base, id: 1, title: "A" }] },
        { id: 2, course_id: 20, course: "Física", num: 1, name: "U1", materials: [{ ...base, id: 2, title: "B" }] },
      ],
    });
    render(<Materials aiStopReason={null} />);
    await screen.findByText("Matemática");
    const links = screen.getAllByRole("link", { name: "Exportar a Anki" });
    // La exportación general va al final de la página.
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "/api/export/anki.apkg?course_id=10",
      "/api/export/anki.apkg?course_id=20",
      "/api/export/anki.apkg",
    ]);
  });
});

describe("Materials: practicar esta clase", () => {
  it("el botón principal practica los temas de la clase; el resumen queda como secundario", async () => {
    const base = { kind: "youtube", error: null, youtube_id: null, campus_url: null, status: "done" };
    mockApi({
      "GET /api/materials": [
        { id: 1, course_id: 1, course: "Matemática", num: 1, name: "UNIDAD 1",
          materials: [
            { ...base, id: 1, title: "Clase 1", summary_md: "Pendiente y ordenada.", topic_ids: [4], questions: 6 },
            { ...base, id: 2, title: "Clase 2", summary_md: null, topic_ids: [5], questions: 0 },
          ] },
      ],
    });
    const calls: unknown[] = [];
    render(<Materials aiStopReason={null} onPractice={(p) => calls.push(p)} />);
    await screen.findByText("Clase 1");
    // Sin preguntas no hay nada que practicar.
    expect(screen.getAllByRole("button", { name: "Practicar esta clase" })).toHaveLength(1);
    expect(screen.queryByText("Pendiente y ordenada.")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Ver resumen" }));
    expect(screen.getByText("Pendiente y ordenada.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Practicar esta clase" }));
    expect(calls).toEqual([{ topic_id: 4 }]);
  });
});
