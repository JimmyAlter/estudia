import { useEffect, useId, useState, type ReactNode } from "react";
import { api, type Age, type AppState, type Material, type QuizParams, type Section } from "../api";
import { keepMath, tidy } from "../format";
import { Button, Label, Notice } from "../ui";

const quietLink = "text-muted underline decoration-line underline-offset-4 hover:text-ink";

type StopReason = AppState["ai_stop_reason"];
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
  const anyFailed = sections.some((s) => s.materials.some((m) => m.status === "failed" || m.status === "needs_public_link"));

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
                    <MaterialRow key={m.id} material={m} stopReason={aiStopReason} age={age} onPractice={onPractice} />
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
  switch (material.status) {
    case "done":
      return null;
    case "unsupported":
      return material.campus_url ? (
        <a href={material.campus_url} target="_blank" rel="noreferrer" className={linkClass}>
          Abrir en el campus
        </a>
      ) : (
        <span className="shrink-0 text-sm text-muted">Solo en el campus</span>
      );
    case "needs_public_link":
      // Sin IA no se va a preparar igual: la pista de la descarga no aplica.
      if (noAi(age)) return <span className="shrink-0 text-sm text-muted">Sin preparar</span>;
      return null; // la explicación va debajo del título
    case "pending":
      if (noAi(age)) return <span className="shrink-0 text-sm text-muted">Sin preparar</span>;
      return (
        <span className="shrink-0 text-sm text-muted">
          {stopReason === "quota" || stopReason === "youtube_cap" ? "Se prepara mañana" : "En preparación"}
        </span>
      );
    default:
      return <span className="shrink-0 text-sm text-muted">No se pudo preparar</span>;
  }
}

function MaterialRow({
  material,
  stopReason,
  age,
  onPractice,
}: {
  material: Material;
  stopReason: StopReason;
  age?: Age | null;
  onPractice?: (params: QuizParams) => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const status = <Status material={material} stopReason={stopReason} age={age} />;
  const canPractice = Boolean(onPractice && material.questions && material.topic_ids?.length);
  // Apagado solo lo que no se puede abrir ahora: lo que falta preparar o falló.
  const unavailable = material.status === "pending" || material.status === "failed" || material.status === "needs_public_link";

  return (
    <li className="border-b border-line py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <span className={`min-w-0 ${unavailable ? "text-muted" : ""}`}>{tidy(material.title)}</span>
        {(canPractice || material.summary_md || material.status !== "done") && (
          <span className="flex shrink-0 items-baseline gap-5 text-sm">
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
                Practicar esta clase
              </button>
            )}
            {material.summary_md && (
              <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                aria-expanded={open}
                aria-controls={`${id}-summary`}
                className="press cursor-pointer text-muted underline decoration-line underline-offset-4 hover:text-ink"
              >
                {open ? "Ocultar resumen" : "Ver resumen"}
              </button>
            )}
          </span>
        )}
      </div>
      {material.status === "needs_public_link" && !noAi(age) && (
        <p className="mt-1 text-sm text-muted">
          Este video no es público. Pedile a la cátedra el archivo en el campus, o activá la descarga en{" "}
          <a href="#/ajustes" className="underline decoration-line underline-offset-4 hover:text-ink">
            Ajustes
          </a>
        </p>
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

/** Markdown simple de los resúmenes: títulos, viñetas y párrafos. Sin HTML. */
function Summary({ markdown, title }: { markdown: string; title: string }) {
  const blocks: { kind: "h" | "li" | "p"; text: string }[] = [];
  for (const raw of markdown.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    const bullet = line.match(/^[-*]\s+(.*)$/);
    if (heading) blocks.push({ kind: "h", text: heading[1] });
    else if (bullet) blocks.push({ kind: "li", text: bullet[1] });
    else blocks.push({ kind: "p", text: line });
  }

  // El primer título suele repetir el nombre de la clase, que ya está arriba.
  if (blocks[0]?.kind === "h" && sameTitle(blocks[0].text, title)) blocks.shift();

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
        <h3 key={out.length} className="mt-4 mb-1 font-semibold first:mt-0">
          {inline(block.text)}
        </h3>
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
