import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
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

    expect(await screen.findByText("Por qué es −5")).toBeInTheDocument();
    // Las opciones se muestran mezcladas, pero se manda el índice original.
    const answerCall = fetchMock.mock.calls.find(([url]) => url === "/api/answer");
    expect(JSON.parse(String(answerCall![1]!.body))).toEqual({ question_id: 1, chosen_index: 1 });
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

  it("\"Reportar error en la pregunta\" la retira y pasa a la siguiente", async () => {
    const fetchMock = mockApi({
      "POST /api/quiz": QUIZ,
      "GET /api/materials": [],
      "POST /api/question/1/retire": { ok: true },
    });
    render(<Practice params={{}} onExit={() => {}} />);
    await screen.findByText(QUIZ[0].stem);

    await userEvent.click(screen.getByRole("button", { name: "Reportar error en la pregunta" }));

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

    await userEvent.click(screen.getByRole("button", { name: "Reportar error en la pregunta" }));
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
    const topic = { id: 3, name: "Funciones cuadráticas", mastery: 0, practiced: false };
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

describe("Home: barra de dominio", () => {
  it("la barra llega justo al porcentaje que se muestra", async () => {
    const topic = { id: 1, name: "Rectas", mastery: 0.666, practiced: true };
    mockApi({
      "GET /api/home": {
        exam: null,
        topics: [topic],
        suggestion: null,
        processing: { pending: 0, done: 1, failed: 0 },
      },
    });
    render(<Home refreshKey={0} aiStopReason={null} working={false} onPractice={() => {}} onNavigate={() => {}} onSessionExpired={() => {}} />);
    expect(await screen.findByText("67% bien")).toBeInTheDocument();
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
        screen.getByText(/En el plan gratuito, Google puede usar lo que le envíes para mejorar sus productos\./),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(/Google pide tener 18 años o más para usar Gemini\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /clave/i })).toHaveAttribute(
      "href",
      "https://aistudio.google.com/apikey",
    );
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
    expect(screen.getByText("se prepara mañana")).toBeInTheDocument();
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
    expect(await screen.findByText("no se pudo preparar")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Reintentar las que fallaron" }));

    expect(await screen.findByText("en preparación")).toBeInTheDocument();
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
