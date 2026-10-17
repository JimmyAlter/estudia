import { useEffect, useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { api, type AppState, type Material, type Section } from "../api";
import { keepMath, tidy } from "../format";
import { Button, Label, Notice } from "../ui";

type StopReason = AppState["ai_stop_reason"];

export default function Materials({ aiStopReason }: { aiStopReason: StopReason }) {
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
  const anyFailed = sections.some((s) => s.materials.some((m) => m.status === "failed"));

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
          {courses.length > 1 && course && <Label className="mb-6">{tidy(course)}</Label>}
          {sections
            .filter((s) => (s.course ?? "") === course)
            .map((section) => (
              <section key={section.id} className="mb-12">
                <h2 className="mb-2 font-semibold">{tidy(section.name)}</h2>
                <ul>
                  {section.materials.map((m) => (
                    <MaterialRow key={m.id} material={m} stopReason={aiStopReason} />
                  ))}
                </ul>
              </section>
            ))}
        </div>
      ))}
    </div>
  );
}

/** Estado de la clase; nada si ya está lista (es lo normal). */
function Status({ material, stopReason }: { material: Material; stopReason: StopReason }) {
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
        <span className="shrink-0 text-sm text-muted">solo en el campus</span>
      );
    case "pending":
      return (
        <span className="shrink-0 text-sm text-muted">{stopReason === "quota" ? "se prepara mañana" : "en preparación"}</span>
      );
    default:
      return <span className="shrink-0 text-sm text-muted">no se pudo preparar</span>;
  }
}

function MaterialRow({ material, stopReason }: { material: Material; stopReason: StopReason }) {
  const status = <Status material={material} stopReason={stopReason} />;

  if (!material.summary_md) {
    return (
      <li className="flex items-baseline justify-between gap-6 border-b border-line py-3 pl-6">
        <span className={material.status === "done" ? "" : "text-muted"}>{tidy(material.title)}</span>
        {status}
      </li>
    );
  }

  return (
    <li className="border-b border-line">
      <details className="group">
        <summary className="flex cursor-pointer list-none items-baseline justify-between gap-6 py-3 [&::-webkit-details-marker]:hidden">
          <span className="flex items-baseline gap-2">
            <ChevronRight
              size={14}
              strokeWidth={1.75}
              aria-hidden
              className="relative top-[2px] shrink-0 text-muted transition-transform duration-150 group-open:rotate-90"
            />
            <span className="group-hover:underline group-hover:decoration-line group-hover:underline-offset-4">
              {tidy(material.title)}
            </span>
          </span>
          {status}
        </summary>
        <div className="reveal pb-6 pl-6">
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
      </details>
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
