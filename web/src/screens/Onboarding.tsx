import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, api, type AppState, type Site } from "../api";
import { Button, Label } from "../ui";
import { AgeStep } from "./Age";
import KeyWizard from "./KeyWizard";

const POLL_MS = 2000;

interface Props {
  state: AppState;
  /** Algo cambió en el servidor: hay que volver a leer el estado. */
  onChange: () => void;
  /** La materia de ejemplo quedó abierta: la app la muestra. Sin esto no se ofrece. */
  onDemo?: () => void;
  /** Se llegó desde la materia de ejemplo ("Conectar mi campus"): se puede volver a ella. */
  onBackToDemo?: () => void;
}

const STEP_NUMBER = { facultad: 1, login: 2, edad: 3, ia: 3, listo: 3 } as const;

export default function Onboarding({ state, onChange, onDemo, onBackToDemo }: Props) {
  return (
    <main className="mx-auto max-w-[30rem] px-6 pt-[14vh] pb-16">
      <div className="mb-14 flex items-baseline justify-between gap-4">
        <div className="font-serif text-xl tracking-tight">Estudia</div>
        {onBackToDemo && (
          <Button variant="quiet" className="text-sm" onClick={onBackToDemo}>
            Volver al ejemplo
          </Button>
        )}
      </div>
      <Label className="mb-3">Paso {STEP_NUMBER[state.step]} de 3</Label>
      {state.step === "facultad" && (
        // Desde la materia de ejemplo no se la vuelve a ofrecer: ya está abierta.
        <ChooseSite onDone={onChange} onDemo={onBackToDemo ? undefined : onDemo} />
      )}
      {state.step === "login" && <CampusLogin onDone={onChange} />}
      {state.step === "edad" && <AgeStep onDone={onChange} heading={<Heading>¿Tenés 18 años o más?</Heading>} />}
      {(state.step === "ia" || state.step === "listo") && (
        <KeyWizard rejected={state.ai_stop_reason === "auth"} onDone={onChange} />
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

function ChooseSite({ onDone, onDemo }: { onDone: () => void; onDemo?: () => void }) {
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
    <>
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
      {onDemo && <TryDemo onDemo={onDemo} />}
    </>
  );
}

/** Probar sin campus ni clave: abre la materia de ejemplo y va derecho a la primera pregunta. */
function TryDemo({ onDemo }: { onDemo: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await api.startDemo();
      onDemo();
    } catch {
      setError("No se pudo abrir la materia de ejemplo. Probá de nuevo.");
      setBusy(false);
    }
  }

  return (
    <div className="mt-12 border-t border-line pt-6">
      <Button variant="quiet" onClick={start} disabled={busy}>
        Probar con una materia de ejemplo
      </Button>
      <p className="mt-2 text-sm text-muted">
        Sin campus, sin clave y sin internet. Lo que contestes queda aparte de tus materias.
      </p>
      <ErrorLine message={error} />
    </div>
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
