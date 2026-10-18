import { File, FileText, Presentation, Video, type LucideIcon } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";
import { api, type Age, type AppState, type Material, type QuizParams, type Section } from "../api";
import { keepMath, tidy } from "../format";
import { Button, Label, Notice, campusHref } from "../ui";
import Recall from "./Recall";

const quietLink = "text-muted underline decoration-line underline-offset-4 hover:text-ink";

type StopReason = AppState["ai_stop_reason"];

// Texto que manda el servidor en `error` (material/files.py, UNREADABLE_TEXT).
const UNREADABLE_TEXT = "No se pudo leer";
// `fail_kind` de un archivo que no se pudo bajar (material/files.py, FAIL_DOWNLOAD).
const FAIL_DOWNLOAD = "download";
/** Un archivo del campus que no se pudo bajar (y no una clase que no se pudo preparar). */
const downloadFailed = (m: Material) => m.status === "failed" && m.fail_kind === FAIL_DOWNLOAD;

const KIND_ICONS: Record<string, LucideIcon> = { pdf: FileText, slides: Presentation, youtube: Video };
/** Sin IA (menor o sin responder) lo pendiente no se va a procesar. */
const noAi = (age?: Age | null) => age === "minor" || age === null;

export default function Materials({
  aiStopReason,
  age,
  onPractice,
}: {
  aiStopReason: StopReason;
  age?: Age | null;
  onPractice?: (params: QuizParams) => void;
}) {
  const [sections, setSections] = useState<Section[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .materials()
      .then((list) => alive && setSections(list))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [reload]);

  async function retry() {
    setRetrying(true);
    setRetryError(false);
    try {
      await api.retryFailed();
      setReload((n) => n + 1);
    } catch {
      setRetryError(true);
    }
    setRetrying(false);
  }

  if (failed) return <Notice>No se pudieron cargar los materiales. Probá recargar la página.</Notice>;
  if (!sections) return null;
  if (!sections.length) {
    return (
      <div className="max-w-xl">
        <h1 className="mb-3 font-serif text-[2rem] leading-tight tracking-[-0.02em]">Materiales</h1>
        <Notice>Todavía no hay material. Aparece después de sincronizar con el campus.</Notice>
      </div>
    );
  }

  const courses = [...new Set(sections.map((s) => s.course ?? ""))];
  const courseIds = new Map(sections.map((s) => [s.course ?? "", s.course_id]));
  // Lo que pide un enlace público también se reintenta (con la descarga prendida o pasados 7 días).
  // Arriba se ofrece desde dos: una sola se ve en su fila.
  const retryable = sections.reduce(
    (n, s) => n + s.materials.filter((m) => m.status === "failed" || m.status === "needs_public_link").length,
    0,
  );
  const anyFailed = retryable >= 2;

  return (
    <div className="max-w-3xl">
      <h1 className={`font-serif text-[2rem] leading-tight tracking-[-0.02em] ${anyFailed ? "mb-3" : "mb-12"}`}>
        Materiales
      </h1>
      {anyFailed && (
        <p className="mb-12 text-sm">
          <Button variant="quiet" onClick={retry} disabled={retrying}>
            Reintentar las que fallaron
          </Button>
          {retryError && (
            <span role="alert" className="ml-3 text-bad">
              No se pudo reintentar. Probá de nuevo.
            </span>
          )}
        </p>
      )}
      {courses.map((course) => (
        <div key={course} className="mb-14">
          {courses.length > 1 && course && (
            <div className="mb-6 flex items-baseline justify-between gap-6">
              <Label>{tidy(course)}</Label>
              <a href={api.ankiUrl(courseIds.get(course))} download className={`text-sm ${quietLink}`}>
                Exportar a Anki
              </a>
            </div>
          )}
          {sections
            .filter((s) => (s.course ?? "") === course)
            .map((section) => (
              <section key={section.id} className="mb-12">
                <h2 className="mb-2 font-semibold">{tidy(section.name)}</h2>
                <ul>
                  {section.materials.map((m) => (
                    <MaterialRow
                      key={m.id}
                      material={m}
                      stopReason={aiStopReason}
                      age={age}
                      onPractice={onPractice}
                      onRetry={retry}
                      retrying={retrying}
                    />
                  ))}
                </ul>
              </section>
            ))}
        </div>
      ))}
      <p className="border-t border-line pt-4 text-sm">
        <a href={api.ankiUrl()} download className={quietLink}>
          Exportar a Anki
        </a>
        <span className="text-muted"> · todas las preguntas verificadas, para repasar en Anki</span>
      </p>
    </div>
  );
}

/** Estado de la clase; nada si ya está lista (es lo normal). */
function Status({ material, stopReason, age }: { material: Material; stopReason: StopReason; age?: Age | null }) {
  const linkClass = "shrink-0 text-sm text-muted underline decoration-line underline-offset-4 hover:text-ink";
  const href = campusHref(material.campus_url);
  // Lo que Estudia no lee (muy grande, ilegible o de otro formato) se abre en el campus: una sola
  // línea, sin explicar por qué. Sin una página segura a la que ir, queda el texto solo.
  const onlyInCampus = href ? (
    <a href={href} target="_blank" rel="noopener noreferrer" className={linkClass}>
      Solo en el campus
    </a>
  ) : (
    <span className="shrink-0 text-sm text-muted">Solo en el campus</span>
  );
  switch (material.status) {
    case "done":
      return null;
    case "downloading":
      return <span className="shrink-0 text-sm text-muted">Se baja del campus</span>;
    case "too_large":
    case "unreadable":
    case "unsupported":
      return onlyInCampus;
    case "needs_public_link":
      // Sin IA no se va a preparar igual: la pista de la descarga no aplica.
      if (noAi(age)) return <span className="shrink-0 text-sm text-muted">Sin preparar</span>;
      return null; // la explicación va debajo del título
    case "pending":
      if (noAi(age)) return <span className="shrink-0 text-sm text-muted">Sin preparar</span>;
      return (
        <span className="shrink-0 text-sm text-muted">
          {stopReason === "quota" || stopReason === "youtube_cap" || stopReason === "budget" ? "Se prepara mañana" : "En preparación"}
        </span>
      );
    default:
      if (material.error === UNREADABLE_TEXT) return onlyInCampus;
      // La explicación y "Reintentar" van debajo del título.
      if (downloadFailed(material)) return <span className="shrink-0 text-sm text-muted">No se pudo bajar</span>;
      return <span className="shrink-0 text-sm text-muted">No se pudo preparar</span>;
  }
}

function MaterialRow({
  material,
  stopReason,
  age,
  onPractice,
  onRetry,
  retrying,
}: {
  material: Material;
  stopReason: StopReason;
  age?: Age | null;
  onPractice?: (params: QuizParams) => void;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  const id = useId();
  /** Lo que se ve debajo de la clase: nada, el resumen o "Contá lo que te acordás". */
  const [view, setView] = useState<"closed" | "summary" | "recall" | "recalled">("closed");
  const open = view === "summary";
  // Sin títulos en el resumen no hay con qué comparar.
  const canRecall = Boolean(material.summary_md && summaryHeadings(material.summary_md).length);
  const status = <Status material={material} stopReason={stopReason} age={age} />;
  const canPractice = Boolean(onPractice && material.questions && material.topic_ids?.length);
  // Apagado solo lo que no se puede abrir ahora: lo que falta preparar o falló.
  const unavailable = ["pending", "downloading", "failed", "needs_public_link"].includes(material.status);
  const Icon = KIND_ICONS[material.kind] ?? File;
  const part = material.part;
  const pageRange = part ? `págs. ${part.from}–${part.to}` : null;
  // El tramo sin capítulo se llama "Apunte 3, págs. 1–40": las páginas van en su propia línea.
  const unnamed = Boolean(pageRange && material.title.endsWith(`, ${pageRange}`));
  const title = tidy(unnamed ? material.title.slice(0, -pageRange!.length - 2) : material.title);
  // Con capítulo, la segunda línea dice de qué apunte es: "Apunte 3, págs. 1–40".
  const partLine = pageRange && !unnamed && part?.doc ? `${tidy(part.doc)}, ${pageRange}` : pageRange;

  return (
    <li className="border-b border-line py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        {/* El ícono cuelga a la izquierda: un título largo sigue alineado con su primera línea. */}
        <span className={`min-w-0 pl-6 -indent-6 ${unavailable ? "text-muted" : ""}`}>
          <Icon size={16} aria-hidden className="mr-2 inline-block align-[-2px] text-muted" />
          {title}
          {partLine && <span className="block indent-0 text-sm text-muted tabular-nums">{partLine}</span>}
        </span>
        {(canPractice || material.summary_md || material.status !== "done") && (
          <span className="flex basis-full flex-wrap items-baseline gap-x-5 gap-y-1 pl-6 text-sm sm:basis-auto">
            {status}
            {canPractice && (
              <button
                type="button"
                onClick={() =>
                  onPractice!(
                    material.topic_ids.length === 1 ? { topic_id: material.topic_ids[0] } : { topic_ids: material.topic_ids },
                  )
                }
                className="press cursor-pointer font-medium underline decoration-ink/40 underline-offset-4 hover:decoration-ink"
              >
                {material.kind === "pdf" || material.kind === "slides" ? "Practicar este apunte" : "Practicar esta clase"}
              </button>
            )}
            {canRecall && view === "closed" && (
              <button
                type="button"
                onClick={() => setView("recall")}
                aria-controls={`${id}-summary`}
                className="press cursor-pointer text-muted underline decoration-line underline-offset-4 hover:text-ink"
              >
                Contá lo que te acordás
              </button>
            )}
            {/* Mientras se escribe no se ofrece: Saltear y Terminar ya son la salida. */}
            {material.summary_md && view !== "recall" && (
              <button
                type="button"
                onClick={() => setView((v) => (v === "closed" ? "summary" : "closed"))}
                aria-expanded={view !== "closed"}
                aria-controls={`${id}-summary`}
                className="press cursor-pointer text-muted underline decoration-line underline-offset-4 hover:text-ink"
              >
                {view === "closed" ? "Ver resumen" : "Ocultar resumen"}
              </button>
            )}
          </span>
        )}
      </div>
      {downloadFailed(material) && (
        <p className="mt-1 pl-6 text-sm text-muted">
          Puede ser la conexión con el campus. Probá de nuevo ahora o más tarde.{" "}
          {onRetry && (
            <Button variant="quiet" onClick={onRetry} disabled={retrying}>
              Reintentar
            </Button>
          )}
        </p>
      )}
      {material.status === "needs_public_link" && !noAi(age) && (
        <p className="mt-1 pl-6 text-sm text-muted">
          Este video no es público. Pedile a la cátedra el archivo en el campus, o activá la descarga en{" "}
          <a href="#/ajustes" className="underline decoration-line underline-offset-4 hover:text-ink">
            Ajustes
          </a>
        </p>
      )}
      {material.summary_md && (view === "recall" || view === "recalled") && (
        <div id={`${id}-summary`} className="reveal pt-4 pb-3">
          <Recall
            materialId={material.id}
            summary={(missed) => <Summary markdown={material.summary_md!} title={material.title} missed={missed} />}
            onSkip={() => setView("summary")}
            onCompared={() => setView("recalled")}
          />
        </div>
      )}
      {material.summary_md && open && (
        <div id={`${id}-summary`} className="reveal pt-4 pb-3">
          <Summary markdown={material.summary_md} title={material.title} />
          {material.youtube_id && (
            <a
              href={`https://youtu.be/${material.youtube_id}`}
              target="_blank"
              rel="noreferrer"
              className="mt-4 inline-block text-sm underline decoration-line underline-offset-4 hover:decoration-ink"
            >
              Ver la clase en YouTube
            </a>
          )}
        </div>
      )}
    </li>
  );
}

/** La marca de página de un documento al final de un título: "Límites (pág. 14)" o "(diap. 4)". */
const PAGE_MARK = /\s*\((?:pág|diap)\. \d+\)$/;

/** El texto de un título como lo compara el servidor: sin los # del final, ni los **, ni el "(pág. N)" o "(diap. N)". */
function headingTitle(text: string): string {
  return text.replace(/[ \t]+#+$/, "").replaceAll("**", "").trim().replace(PAGE_MARK, "").trim();
}

function summaryHeadings(markdown: string): string[] {
  return markdown
    .split("\n")
    .map((raw) => raw.trim().match(/^#{1,6}\s+(.*)$/))
    .filter((m): m is RegExpMatchArray => m !== null)
    .map((m) => headingTitle(m[1]));
}

/**
 * Markdown simple de los resúmenes: títulos, viñetas y párrafos. Sin HTML. Con `missed`, los títulos
 * que no aparecieron en "Contá lo que te acordás" llevan "No lo mencionaste".
 */
function Summary({ markdown, title, missed }: { markdown: string; title: string; missed?: Set<string> }) {
  const blocks: { kind: "h" | "li" | "p"; text: string }[] = [];
  for (const raw of markdown.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    const bullet = line.match(/^[-*]\s+(.*)$/);
    if (heading) blocks.push({ kind: "h", text: heading[1].replace(/[ \t]+#+$/, "") });
    else if (bullet) blocks.push({ kind: "li", text: bullet[1] });
    else blocks.push({ kind: "p", text: line });
  }

  // El primer título suele repetir el nombre de la clase, que ya está arriba.
  // Si quedó sin mencionar, se deja: la marca tiene que verse.
  if (blocks[0]?.kind === "h" && sameTitle(headingTitle(blocks[0].text), title) && !missed?.has(headingTitle(blocks[0].text)))
    blocks.shift();

  const out: ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) {
      out.push(
        <ul key={`ul${out.length}`} className="my-2 list-disc space-y-1 pl-5 marker:text-faint">
          {list.map((text, i) => (
            <li key={i}>{inline(text)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };
  for (const block of blocks) {
    if (block.kind === "li") {
      list.push(block.text);
      continue;
    }
    flush();
    out.push(
      block.kind === "h" ? (
        missed?.has(headingTitle(block.text)) ? (
          <h3 key={out.length} className="mt-4 mb-1 font-semibold first:mt-0">
            <HeadingText text={block.text} />
            <span className="sr-only">, </span>
            <span className="ml-2 font-sans text-sm font-normal whitespace-nowrap text-muted">No lo mencionaste</span>
          </h3>
        ) : (
          <h3 key={out.length} className="mt-4 mb-1 font-semibold first:mt-0">
            <HeadingText text={block.text} />
          </h3>
        )
      ) : (
        <p key={out.length} className="my-2">
          {inline(block.text)}
        </p>
      ),
    );
  }
  flush();
  return <div className="math max-w-[65ch] text-[1.0625rem] leading-relaxed">{out}</div>;
}

/** Un título del resumen; la página del apunte, si la tiene, va en gris. */
function HeadingText({ text }: { text: string }) {
  const mark = text.match(PAGE_MARK);
  if (!mark) return <>{inline(text)}</>;
  return (
    <>
      {inline(text.slice(0, mark.index))}{" "}
      <span className="font-normal whitespace-nowrap text-muted tabular-nums">{mark[0].trim()}</span>
    </>
  );
}

/** "Rectas" repite a "Video-clase 1: Rectas." (o al título entero). */
function sameTitle(heading: string, title: string): boolean {
  const norm = (t: string) => t.toLocaleLowerCase("es").replace(/[.\s]+$/, "").trim();
  const h = norm(heading);
  const full = norm(title);
  return h === full || h === norm(full.split(":").slice(1).join(":"));
}

/** **negrita** dentro de una línea. */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? (
      <strong key={i} className="font-semibold">
        {keepMath(part.slice(2, -2))}
      </strong>
    ) : (
      keepMath(part)
    ),
  );
}
