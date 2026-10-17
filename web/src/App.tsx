import { useCallback, useEffect, useRef, useState } from "react";
import { RotateCw } from "lucide-react";
import { ApiError, api, type AppState, type QuizParams } from "./api";
import { timeAgo } from "./format";
import Home, { type Screen } from "./screens/Home";
import Materials from "./screens/Materials";
import Onboarding, { LOGIN_WAITING_TEXT, PasteCallback, useCampusLogin } from "./screens/Onboarding";
import Practice from "./screens/Practice";
import { Button } from "./ui";

export const POLL_SYNCING_MS = 3000;
export const POLL_IDLE_MS = 30000;
export const POLL_OFFLINE_MS = 5000;

const NAV: { screen: Screen; label: string }[] = [
  { screen: "inicio", label: "Inicio" },
  { screen: "practica", label: "Práctica" },
  { screen: "materiales", label: "Materiales" },
];

function screenFromHash(): Screen {
  const name = window.location.hash.replace(/^#\/?/, "");
  return NAV.some((n) => n.screen === name) ? (name as Screen) : "inicio";
}

export default function App() {
  const [state, setState] = useState<AppState | null>(null);
  const [offline, setOffline] = useState(false);
  const [screen, setScreen] = useState<Screen>(screenFromHash);
  const [practice, setPractice] = useState<QuizParams>({});
  const [refreshKey, setRefreshKey] = useState(0);
  const [sessionExpired, setSessionExpired] = useState(false);
  const lastSync = useRef<number | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const refreshRef = useRef<() => Promise<void>>(async () => {});

  // Lee el estado y agenda la próxima lectura pase lo que pase: si el servidor
  // no responde, se sigue intentando y la app se recupera sola cuando vuelve.
  const refresh = useCallback(async () => {
    window.clearTimeout(timer.current);
    let next: AppState | null = null;
    try {
      next = await api.state();
      setOffline(false);
      setState(next);
      if (next.step !== "login") setSessionExpired(false);
      // Terminó una sincronización: hay datos nuevos para el inicio.
      if (lastSync.current !== null && next.last_sync_at !== lastSync.current) setRefreshKey((k) => k + 1);
      lastSync.current = next.last_sync_at;
    } catch {
      setOffline(true);
    } finally {
      window.clearTimeout(timer.current);
      const delay = next === null ? POLL_OFFLINE_MS : next.syncing ? POLL_SYNCING_MS : POLL_IDLE_MS;
      timer.current = window.setTimeout(() => refreshRef.current(), delay);
    }
  }, []);
  refreshRef.current = refresh;

  useEffect(() => {
    refresh();
    return () => window.clearTimeout(timer.current);
  }, [refresh]);

  useEffect(() => {
    const onHash = () => setScreen(screenFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((to: Screen) => {
    if (to === "practica") setPractice({});
    window.location.hash = `/${to}`;
    setScreen(to);
    window.scrollTo(0, 0);
  }, []);

  const startPractice = useCallback((params: QuizParams) => {
    setPractice(params);
    window.location.hash = "/practica";
    setScreen("practica");
    window.scrollTo(0, 0);
  }, []);

  const onSessionExpired = useCallback(() => {
    setSessionExpired(true);
    refreshRef.current();
  }, []);

  if (!state) {
    return offline ? (
      <main className="mx-auto max-w-xl px-6 py-24">
        <p className="text-muted">Estudia no responde. Se reintenta solo.</p>
      </main>
    ) : null;
  }

  // Sin sesión pero con datos de antes: se sigue estudiando y se ofrece volver a entrar.
  const hasData = state.last_sync_at !== null;
  if (state.step === "facultad" || state.step === "ia" || (state.step === "login" && !hasData)) {
    return <Onboarding state={state} onChange={refresh} />;
  }
  const expired = sessionExpired || state.step === "login";

  return (
    <div className="mx-auto max-w-5xl px-6 pb-24 md:px-10">
      <header className="flex items-baseline gap-x-5 py-7 sm:gap-x-10">
        <span className="font-serif text-lg tracking-tight">Estudia</span>
        <nav className="flex gap-4 sm:gap-6" aria-label="Secciones">
          {NAV.map((item) => (
            <a
              key={item.screen}
              href={`#/${item.screen}`}
              aria-current={screen === item.screen ? "page" : undefined}
              onClick={(e) => {
                e.preventDefault();
                navigate(item.screen);
              }}
              className={
                screen === item.screen
                  ? "font-medium text-ink"
                  : "text-muted transition-colors duration-150 hover:text-ink"
              }
            >
              {item.label}
            </a>
          ))}
        </nav>
        {!expired && <SyncStatus state={state} onChange={refresh} />}
      </header>

      <div className="space-y-2">
        {expired && <ExpiredSession onDone={refresh} />}
        <StatusNotice state={state} offline={offline} />
      </div>

      <main className="pt-10">
        {screen === "inicio" && (
          <Home
            refreshKey={refreshKey}
            aiStopReason={state.ai_stop_reason}
            working={state.syncing && !expired}
            onPractice={startPractice}
            onNavigate={navigate}
            onSessionExpired={onSessionExpired}
          />
        )}
        {screen === "practica" && <Practice params={practice} onExit={() => navigate("inicio")} />}
        {screen === "materiales" && <Materials key={refreshKey} aiStopReason={state.ai_stop_reason} />}
      </main>
    </div>
  );
}

function SyncStatus({ state, onChange }: { state: AppState; onChange: () => void }) {
  const [starting, setStarting] = useState(false);

  async function sync() {
    setStarting(true);
    try {
      await api.sync();
    } catch (err) {
      // 409: ya había una en curso; 401: venció la sesión. En ambos casos el estado lo dice.
      if (!(err instanceof ApiError)) throw err;
    } finally {
      setStarting(false);
      onChange();
    }
  }

  if (state.syncing || starting) {
    return (
      <span aria-live="polite" className="ml-auto text-sm text-muted">
        Actualizando<span className="max-sm:hidden"> con el campus</span>…
      </span>
    );
  }
  return (
    <div className="ml-auto flex items-baseline gap-4 text-sm text-muted">
      {state.last_sync_at && <span className="max-sm:hidden">Actualizado {timeAgo(state.last_sync_at)}</span>}
      <Button variant="quiet" onClick={sync} className="max-sm:hidden">
        Actualizar
      </Button>
      <button
        type="button"
        onClick={sync}
        aria-label="Actualizar con el campus"
        title="Actualizar con el campus"
        className="press relative top-[3px] cursor-pointer text-muted hover:text-ink sm:hidden"
      >
        <RotateCw size={16} strokeWidth={1.75} aria-hidden />
      </button>
    </div>
  );
}

/** Una línea, sin caja: la sesión del campus venció pero se puede seguir estudiando. */
function ExpiredSession({ onDone }: { onDone: () => void }) {
  const { waiting, error, start } = useCampusLogin(onDone);
  return (
    <div className="border-t border-line pt-3 text-sm text-muted">
      <p role="status">
        {waiting ? (
          <>
            {LOGIN_WAITING_TEXT}{" "}
            <Button variant="quiet" onClick={start}>
              Abrir de nuevo
            </Button>
          </>
        ) : (
          <>
            Tu sesión del campus venció. No llega material nuevo hasta que vuelvas a entrar.{" "}
            <Button variant="quiet" onClick={start}>
              Volver a entrar
            </Button>
          </>
        )}
        {error && <span className="ml-2 text-bad">{error}</span>}
      </p>
      {/* Igual que en el primer inicio de sesión: si el navegador no vuelve solo, se pega el enlace. */}
      {waiting && <PasteCallback onDone={onDone} className="mt-4 max-w-xl" />}
    </div>
  );
}

function StatusNotice({ state, offline }: { state: AppState; offline: boolean }) {
  let message: string | null = null;
  if (offline) {
    message = "Estudia no responde. Se reintenta solo.";
  } else if (state.ai_stop_reason === "quota") {
    message = "Por hoy se usó todo lo gratis de Gemini. Mañana se preparan las clases que faltan.";
  } else if (state.ai_stop_reason === "unavailable") {
    message = "Gemini no responde en este momento. Se vuelve a intentar solo.";
  } else if (state.last_sync_error && state.last_sync_error !== "token") {
    message = "No se pudo actualizar con el campus. Se reintenta más tarde.";
  }
  if (!message) return null;
  return (
    <p role="status" className="border-t border-line pt-3 text-sm text-muted">
      {message}
    </p>
  );
}
