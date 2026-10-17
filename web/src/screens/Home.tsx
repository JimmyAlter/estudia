import { useEffect, useState } from "react";
import { ApiError, api, type AppState, type HomeData, type QuizParams, type Topic } from "../api";
import { formatDate, formatDuration, plural, tidy } from "../format";
import { Button, Label, Notice } from "../ui";

export type Screen = "inicio" | "practica" | "materiales";

interface Props {
  /** Cambia cuando hay datos nuevos (terminó una sincronización). */
  refreshKey: number;
  /** Por qué se frenó el procesamiento con Gemini, si se frenó. */
  aiStopReason: AppState["ai_stop_reason"];
  /** Hay una sincronización/preparación corriendo ahora. */
  working: boolean;
  onPractice: (params: QuizParams) => void;
  onNavigate: (screen: Screen) => void;
  onSessionExpired: () => void;
}

export default function Home({ refreshKey, aiStopReason, working, onPractice, onNavigate, onSessionExpired }: Props) {
  const [data, setData] = useState<HomeData | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .home()
      .then((home) => alive && setData(home))
      .catch((err) => {
        if (!alive) return;
        if (err instanceof ApiError && err.status === 401) onSessionExpired();
        else setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [refreshKey, onSessionExpired]);

  if (failed) return <Notice>No se pudo cargar el inicio. Probá recargar la página.</Notice>;
  if (!data) return null;

  const { exam, topics, suggestion } = data;

  return (
    <div>
      <section className="mb-14">
        {exam ? (
          <>
            <Label className="mb-2">Próximo examen</Label>
            <h1 className="font-serif text-[2.6rem] leading-[1.1] tracking-[-0.025em]">{tidy(exam.name)}</h1>
            <p className="mt-3 text-muted">
              {[
                tidy(exam.course),
                `abre el ${formatDate(exam.opens_at)}`,
                `${plural(exam.attempts, "intento", "intentos")}, ${formatDuration(exam.time_limit_s)}`,
              ].join(" · ")}
            </p>
            <p className="mt-1 font-medium">{daysLeft(exam.days_left)}</p>
          </>
        ) : (
          <>
            <h1 className="font-serif text-[2.6rem] leading-[1.1] tracking-[-0.025em]">Sin exámenes próximos</h1>
            <p className="mt-3 text-muted">Cuando el campus publique uno, aparece acá.</p>
          </>
        )}
      </section>

      <div className="grid gap-x-16 gap-y-12 md:grid-cols-[minmax(0,1fr)_15rem]">
        <section>
          <h2 className="mb-3 font-semibold">{exam ? "Temas que entran" : "Temas"}</h2>
          {topics.length ? (
            <ul>
              {topics.map((topic) => (
                <TopicRow key={topic.id} topic={topic} onClick={() => onPractice({ topic_id: topic.id })} />
              ))}
            </ul>
          ) : (
            <Notice>Todavía no hay temas. Aparecen a medida que se procesan las clases.</Notice>
          )}
        </section>

        <aside className="max-md:order-first">
          <h2 className="mb-3 font-semibold">Seguir estudiando</h2>
          {suggestion ? (
            <>
              <p className="mb-5 text-muted">
                {suggestion.practiced ? "Lo más flojo ahora:" : "Todavía no practicaste:"}{" "}
                <span className="text-ink">{suggestion.name}</span>.
              </p>
              <div className="flex flex-col items-start gap-3">
                <Button onClick={() => onPractice({ topic_id: suggestion.id })}>Practicar ese tema</Button>
                <Button variant="secondary" onClick={() => onPractice({})}>
                  Práctica de todo
                </Button>
                <Button variant="quiet" className="mt-1" onClick={() => onNavigate("materiales")}>
                  Leer los resúmenes
                </Button>
              </div>
            </>
          ) : (
            <Button variant="secondary" onClick={() => onNavigate("materiales")}>
              Ver materiales
            </Button>
          )}
          <Processing processing={data.processing} stopReason={aiStopReason} working={working} />
        </aside>
      </div>
    </div>
  );
}

function daysLeft(days: number): string {
  if (days === 0) return "Es hoy";
  if (days === 1) return "Falta 1 día";
  return `Faltan ${days} días`;
}

function TopicRow({ topic, onClick }: { topic: Topic; onClick: () => void }) {
  const percent = Math.round(topic.mastery * 100);
  return (
    <li className="border-b border-line">
      <button
        type="button"
        onClick={onClick}
        title={`Practicar ${topic.name}`}
        className="group -mx-2 grid w-[calc(100%+1rem)] cursor-pointer grid-cols-[minmax(0,1fr)_6.5rem] items-baseline gap-6 px-2 py-3 text-left"
      >
        <span>
          <span className="block group-hover:underline group-hover:decoration-line group-hover:underline-offset-4">
            {topic.name}
          </span>
          <span className="mt-2 block h-1 overflow-hidden rounded-full bg-track" aria-hidden>
            <span
              data-testid="mastery-bar"
              className="block h-full rounded-full bg-ink/75"
              // Mismo número que el texto: la barra llega justo al porcentaje que se muestra.
              style={{ width: `${percent}%` }}
            />
          </span>
        </span>
        <span className="text-right text-sm tabular-nums text-muted">
          {topic.practiced ? `${percent}% bien` : "sin practicar"}
        </span>
      </button>
    </li>
  );
}

function Processing({
  processing,
  stopReason,
  working,
}: {
  processing: HomeData["processing"];
  stopReason: AppState["ai_stop_reason"];
  working: boolean;
}) {
  const { pending, done, failed } = processing;
  if (!pending && !done && !failed) return null;
  let line: string | null = null;
  if (!pending) line = done ? plural(done, "clase lista", "clases listas") : null;
  // Con la cuota agotada, el aviso de arriba ya lo dice.
  else if (stopReason === "quota") line = null;
  else if (stopReason === "unavailable") line = `${plural(pending, "clase", "clases")} en espera`;
  // "Preparando" solo si de verdad hay algo corriendo; si no, no se promete avance.
  else if (working) line = `Preparando ${plural(pending, "clase", "clases")}`;
  else line = `${plural(pending, "clase", "clases")} por preparar`;
  if (!line && !failed) return null;
  return (
    <div className="mt-10 space-y-1 text-sm text-muted">
      {line && <p>{line}</p>}
      {failed > 0 && <p>{plural(failed, "clase no se pudo procesar", "clases no se pudieron procesar")}</p>}
    </div>
  );
}
