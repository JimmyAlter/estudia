import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, api, type AppState, type Site } from "../api";
import { Button, Label } from "../ui";
import { AgeStep } from "./Age";

const AI_STUDIO_URL = "https://aistudio.google.com/apikey";
const POLL_MS = 2000;

interface Props {
  state: AppState;
  /** Algo cambió en el servidor: hay que volver a leer el estado. */
  onChange: () => void;
}

const STEP_NUMBER = { facultad: 1, login: 2, edad: 3, ia: 3, listo: 3 } as const;

export default function Onboarding({ state, onChange }: Props) {
  return (
    <main className="mx-auto max-w-[30rem] px-6 pt-[14vh] pb-16">
      <div className="mb-14 font-serif text-xl tracking-tight">Estudia</div>
      <Label className="mb-3">Paso {STEP_NUMBER[state.step]} de 3</Label>
      {state.step === "facultad" && <ChooseSite onDone={onChange} />}
      {state.step === "login" && <CampusLogin onDone={onChange} />}
      {state.step === "edad" && <AgeStep onDone={onChange} heading={<Heading>¿Tenés 18 años o más?</Heading>} />}
      {(state.step === "ia" || state.step === "listo") && (
        <ConnectAI rejected={state.ai_stop_reason === "auth"} onDone={onChange} />
      )}
    </main>
  );
}

function Heading({ children }: { children: ReactNode }) {
  return <h1 className="mb-3 font-serif text-[2rem] leading-tight tracking-[-0.02em]">{children}</h1>;
}

function ErrorLine({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="mt-4 text-sm text-bad">
      {message}
    </p>
  );
}

function ChooseSite({ onDone }: { onDone: () => void }) {
  const [sites, setSites] = useState<Site[] | null>(null);
  const [siteId, setSiteId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .catalog()
      .then((list) => {
        setSites(list);
        if (list.length === 1) setSiteId(list[0].id);
      })
      .catch(() => setError("No se pudo cargar la lista de facultades."));
  }, []);

  const universities = [...new Set((sites ?? []).map((s) => s.university))];

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!siteId) return;
    setBusy(true);
    setError(null);
    try {
      await api.chooseSite(siteId);
      onDone();
    } catch {
      setError("No se pudo guardar la facultad. Probá de nuevo.");
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <Heading>¿En qué facultad estudiás?</Heading>
      <p className="mb-8 text-muted">Estudia se conecta con el campus virtual de tu facultad.</p>
      <label htmlFor="site" className="mb-2 block text-sm font-medium">
        Facultad
      </label>
      <select
        id="site"
        value={siteId}
        onChange={(e) => setSiteId(e.target.value)}
        disabled={!sites}
        className="mb-6 w-full cursor-pointer rounded-md border border-field bg-canvas px-3 py-2.5"
      >
        <option value="" disabled>
          {sites ? "Elegí tu facultad" : "Cargando…"}
        </option>
        {universities.map((university) => (
          <optgroup key={university} label={university}>
            {sites!
              .filter((s) => s.university === university)
              .map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
          </optgroup>
        ))}
      </select>
      <Button type="submit" disabled={!siteId || busy}>
        Seguir
      </Button>
      <ErrorLine message={error} />
    </form>
  );
}

/** Login en el campus: abre el navegador y consulta el estado hasta que vuelve el token. */
export function useCampusLogin(onDone: () => void) {
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(async () => {
      try {
        const state = await api.state();
        if (state.step !== "login") onDone();
      } catch {
        // El servidor puede estar ocupado un momento; se reintenta en el próximo ciclo.
      }
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [waiting, onDone]);

  async function start() {
    setError(null);
    try {
      await api.startLogin();
      setWaiting(true);
    } catch {
      setError("No se pudo abrir el campus. Probá de nuevo.");
    }
  }

  return { waiting, error, start };
}

export const LOGIN_WAITING_TEXT = "Iniciá sesión en la ventana que se abrió. Cuando termines, volvé acá.";

function CampusLogin({ onDone }: { onDone: () => void }) {
  const { waiting, error, start } = useCampusLogin(onDone);
  return (
    <div>
      <Heading>Iniciá sesión en el campus</Heading>
      <p className="mb-8 text-muted">
        Se abre el campus en tu navegador. Estudia solo lee tu material: no entrega ni cambia nada.
      </p>
      {waiting ? (
        <>
          <div aria-live="polite">
            <p className="mb-5">{LOGIN_WAITING_TEXT}</p>
            <Button variant="quiet" onClick={start}>
              Abrir de nuevo
            </Button>
          </div>
          <PasteCallback onDone={onDone} />
        </>
      ) : (
        <Button onClick={start}>Iniciar sesión en el campus</Button>
      )}
      <ErrorLine message={error} />
    </div>
  );
}

/** Plan B si el navegador no devuelve el enlace solo. El enlace lleva el token: no se muestra en mensajes. */
export function PasteCallback({
  onDone,
  className = "mt-12 border-t border-line pt-6",
}: {
  onDone: () => void;
  className?: string;
}) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [fieldError, setFieldError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!url.trim()) {
      setFieldError("Pegá el enlace primero.");
      return;
    }
    setBusy(true);
    setFieldError(null);
    try {
      await api.loginCallback(url.trim());
      onDone();
    } catch (err) {
      setFieldError(
        err instanceof ApiError && err.status === 400
          ? "Ese enlace no sirvió. Tocá “Abrir de nuevo” y probá otra vez."
          : "No se pudo continuar. Probá de nuevo.",
      );
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className={`${className} text-sm`}>
      <label htmlFor="callback-url" className="mb-2 block text-muted">
        ¿No volvió solo? Pegá acá el enlace que empieza con moodlemobile:// o estudia://
      </label>
      <div className="flex gap-3">
        <input
          id="callback-url"
          type="text"
          autoComplete="off"
          spellCheck={false}
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          aria-invalid={fieldError ? true : undefined}
          aria-describedby={fieldError ? "callback-url-error" : undefined}
          className="min-w-0 flex-1 rounded-md border border-field bg-canvas px-3 py-2 font-mono text-sm aria-invalid:border-bad"
        />
        <Button type="submit" variant="secondary" disabled={busy}>
          Continuar
        </Button>
      </div>
      {fieldError && (
        <p id="callback-url-error" role="alert" className="mt-2 text-bad">
          {fieldError}
        </p>
      )}
    </form>
  );
}

function ConnectAI({ rejected, onDone }: { rejected: boolean; onDone: () => void }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  /** Problema con lo que está en el campo (va debajo del campo). */
  const [fieldError, setFieldError] = useState<string | null>(null);
  /** Problema que no es de la clave (Google no respondió). */
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!key.trim()) {
      setFieldError("Pegá la clave primero.");
      input.current?.focus();
      return;
    }
    setBusy(true);
    setFieldError(null);
    try {
      await api.setAiKey(key);
      onDone();
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        setFieldError("Google rechazó esa clave. Revisá que esté completa.");
        input.current?.focus();
      } else {
        setError("No se pudo verificar la clave ahora. Probá en un rato.");
      }
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <Heading>Conectá tu IA</Heading>
      <p className="mb-8 text-muted">
        {rejected
          ? "Google rechazó la clave guardada. Pegá una nueva para seguir procesando tus clases."
          : "Estudia usa Gemini, de Google, para armar resúmenes y preguntas con tus clases. La clave es gratis."}
      </p>
      <ol className="mb-8 space-y-2">
        <li>
          1.{" "}
          <a
            href={AI_STUDIO_URL}
            target="_blank"
            rel="noreferrer"
            className="underline decoration-line underline-offset-4 hover:decoration-ink"
          >
            Creá una clave en Google AI Studio
          </a>
        </li>
        <li>2. Copiala y pegala acá.</li>
      </ol>
      <label htmlFor="ai-key" className="mb-2 block text-sm font-medium">
        Clave de Gemini
      </label>
      <input
        id="ai-key"
        type="password"
        autoComplete="off"
        spellCheck={false}
        ref={input}
        value={key}
        onChange={(e) => {
          setKey(e.target.value);
          // El aviso era sobre lo que había antes: al corregir, se va.
          setFieldError(null);
        }}
        aria-invalid={fieldError ? true : undefined}
        aria-describedby={fieldError ? "ai-key-error" : undefined}
        className="mb-2 w-full rounded-md border border-field bg-canvas px-3 py-2.5 font-mono text-sm aria-invalid:border-bad"
      />
      {fieldError && (
        <p id="ai-key-error" role="alert" className="mb-2 text-sm text-bad">
          {fieldError}
        </p>
      )}
      <div className="mb-6 text-sm text-muted">
        <Label className="mb-1">Qué ve Google</Label>
        <p>Google recibe el video de cada clase y las preguntas que Estudia arma. No recibe tus respuestas ni tus datos personales.</p>
        <p>En el plan gratuito, personas de Google pueden leer lo que se envía y usarlo para mejorar sus productos.</p>
      </div>
      <Button type="submit" disabled={busy}>
        {busy ? "Verificando…" : "Guardar clave"}
      </Button>
      <ErrorLine message={error} />
    </form>
  );
}
