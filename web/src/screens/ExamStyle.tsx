import { useId, useState } from "react";
import { ApiError, api, type ExamStyleData, type ExamStyleView, type ExerciseKind } from "../api";
import { Button, FIELD } from "../ui";

const KINDS: { kind: ExerciseKind; label: string }[] = [
  { kind: "opcion_multiple", label: "Opción múltiple" },
  { kind: "verdadero_falso", label: "Verdadero o falso" },
  { kind: "desarrollo", label: "Desarrollo" },
];

const BIBLIOGRAPHY = "Si cambió la bibliografía, revisá que los temas sigan entrando.";
const CHECK_NUMBERS = "Revisá los números: alguno quedó vacío o es más grande de lo que se puede usar.";

/** 1.5 → "1,5"; null → "". */
const toField = (n: number | null | undefined) => (n == null ? "" : String(n).replace(".", ","));

/** "Series", "Series y Límites", "Series, Sucesiones e Integrales" (la "y" pasa a "e" antes de "i"). */
function listText(items: string[]): string {
  if (items.length < 2) return items.join("");
  const last = items[items.length - 1];
  const and = /^h?[ií](?![aeoáéó])/i.test(last) ? "e" : "y";
  return `${items.slice(0, -1).join(", ")} ${and} ${last}`;
}

/** Lo que el formato trae de los parciales y no es un tema de la materia: no entra en el simulacro. */
function unmatchedText(topics: string[]): string {
  return topics.length === 1
    ? `${topics[0]} no está en la materia, así que no entra en el simulacro.`
    : `${listText(topics)} no están en la materia, así que no entran en el simulacro.`;
}

/** "" → null; "1,5" → 1.5; algo que no es un número → NaN. */
function fromField(text: string): number | null {
  const t = text.trim().replace(",", ".");
  if (!t) return null;
  return /^\d+(\.\d+)?$/.test(t) ? Number(t) : NaN;
}

interface Draft {
  rows: Record<ExerciseKind, { count: string; points: string }>;
  minutes: string;
  maxGrade: string;
  formulaSheet: boolean;
  topicIds: Set<number>;
  unmatched: string[];
}

function draftOf(style: ExamStyleData | null): Draft {
  const rows = Object.fromEntries(KINDS.map(({ kind }) => [kind, { count: "", points: "" }])) as Draft["rows"];
  for (const g of style?.groups ?? []) rows[g.kind] = { count: String(g.count), points: toField(g.points_each) };
  return {
    rows,
    minutes: toField(style?.minutes),
    maxGrade: toField(style?.max_grade),
    formulaSheet: style?.formula_sheet ?? false,
    topicIds: new Set(style?.topic_ids ?? []),
    unmatched: style?.topics_unmatched ?? [],
  };
}

/**
 * El formato de la cátedra: lo que propusieron los parciales (o lo guardado), para revisarlo y
 * usarlo en el simulacro. Se puede cargar todo a mano, también sin IA.
 * `onChange`: se guardó o se borró, así la pantalla vuelve a leerlo.
 */
export default function ExamStyle({ examId, view, onChange }: { examId: number; view: ExamStyleView; onChange: () => void }) {
  const id = useId();
  const [draft, setDraft] = useState(() => draftOf(view.saved ?? view.proposed));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setRow = (kind: ExerciseKind, field: "count" | "points", value: string) =>
    setDraft((d) => ({ ...d, rows: { ...d.rows, [kind]: { ...d.rows[kind], [field]: value } } }));

  /** El formato a guardar, o null si algún número no sirve. */
  function build(): ExamStyleData | null {
    const groups = [];
    for (const { kind } of KINDS) {
      const count = fromField(draft.rows[kind].count);
      if (!count) {
        if (Number.isNaN(count)) return null;
        continue; // vacío o 0: ese tipo no está
      }
      const points = fromField(draft.rows[kind].points);
      if (!Number.isInteger(count) || points === null || Number.isNaN(points) || points <= 0) return null;
      groups.push({ kind, count, points_each: points });
    }
    const minutes = fromField(draft.minutes);
    const maxGrade = fromField(draft.maxGrade);
    if (Number.isNaN(minutes) || (minutes !== null && !Number.isInteger(minutes)) || Number.isNaN(maxGrade)) return null;
    return {
      groups,
      minutes,
      max_grade: maxGrade,
      topic_ids: view.topics.filter((t) => draft.topicIds.has(t.id)).map((t) => t.id),
      topics_unmatched: draft.unmatched,
      formula_sheet: draft.formulaSheet,
    };
  }

  async function save() {
    const style = build();
    if (!style) {
      setError(CHECK_NUMBERS);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.saveExamStyle(examId, style);
      onChange();
    } catch (err) {
      // 400 trae un texto para mostrar; la validación (422) viene en inglés y no se muestra.
      if (err instanceof ApiError && err.status === 400) setError(err.detail);
      else if (err instanceof ApiError && err.status === 422) setError(CHECK_NUMBERS);
      else setError("No se pudo guardar. Probá de nuevo.");
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    setBusy(true);
    setError(null);
    try {
      await api.clearExamStyle(examId);
      onChange();
    } catch {
      setError("No se pudo volver al de siempre. Probá de nuevo.");
    } finally {
      setBusy(false);
    }
  }

  // Propuesto por los parciales (todavía sin guardar): de ahí sale y hay que revisarlo.
  const fromPastExams = !view.saved && Boolean(view.proposed);
  const n = view.sources.length;
  const intro = view.saved
    ? "El simulacro de este examen usa este formato."
    : view.proposed
      ? `${n ? `Sale del promedio de ${n} ${n === 1 ? "parcial" : "parciales"}.` : "Sale de tus parciales."} Revisalo y, si te sirve, usalo en el simulacro.`
      : view.ai
        ? "Se completa solo cuando se lee un parcial. También lo podés cargar a mano."
        : "Cargalo a mano: cuántos ejercicios de cada tipo, cuánto vale cada uno y el tiempo.";

  return (
    <section aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="mb-1 font-semibold">
        Formato de tu cátedra
      </h2>
      <p className="mb-5 text-sm text-muted">{intro}</p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        {/* Una fila por tipo: nombre, cantidad y puntos de cada uno. */}
        <div className="grid max-w-sm grid-cols-[minmax(0,1fr)_4.5rem_6rem] items-center gap-x-3 gap-y-2">
          <span aria-hidden />
          <span aria-hidden className="text-xs text-muted">
            cantidad
          </span>
          <span aria-hidden className="text-xs whitespace-nowrap text-muted">
            puntos cada uno
          </span>
          {KINDS.map(({ kind, label }) => (
            <Row
              key={kind}
              label={label}
              count={draft.rows[kind].count}
              points={draft.rows[kind].points}
              onCount={(v) => setRow(kind, "count", v)}
              onPoints={(v) => setRow(kind, "points", v)}
            />
          ))}
        </div>

        <div className="mt-6 flex flex-wrap gap-x-6 gap-y-4">
          <NumberField
            label="Tiempo (minutos)"
            value={draft.minutes}
            inputMode="numeric"
            onChange={(minutes) => setDraft((d) => ({ ...d, minutes }))}
          />
          <NumberField label="Nota sobre" value={draft.maxGrade} onChange={(maxGrade) => setDraft((d) => ({ ...d, maxGrade }))} />
        </div>

        <label className="mt-5 flex w-fit cursor-pointer items-baseline gap-3">
          <input
            type="checkbox"
            checked={draft.formulaSheet}
            onChange={(e) => setDraft((d) => ({ ...d, formulaSheet: e.target.checked }))}
            className="accent-[var(--ink)]"
          />
          Hay hoja de fórmulas
        </label>

        {view.topics.length > 0 && (
          <fieldset className="mt-6">
            <legend className="mb-2 text-sm font-medium">Temas</legend>
            <div className="space-y-1">
              {view.topics.map((t) => (
                <label key={t.id} className="flex w-fit cursor-pointer items-baseline gap-3 py-0.5">
                  <input
                    type="checkbox"
                    checked={draft.topicIds.has(t.id)}
                    onChange={(e) =>
                      setDraft((d) => {
                        const topicIds = new Set(d.topicIds);
                        if (e.target.checked) topicIds.add(t.id);
                        else topicIds.delete(t.id);
                        return { ...d, topicIds };
                      })
                    }
                    className="accent-[var(--ink)]"
                  />
                  {t.name}
                </label>
              ))}
            </div>
          </fieldset>
        )}
        {draft.unmatched.length > 0 && <p className="mt-3 text-sm text-muted">{unmatchedText(draft.unmatched)}</p>}
        {fromPastExams && <p className="mt-3 text-sm text-muted">{BIBLIOGRAPHY}</p>}

        {error && (
          <p role="alert" className="mt-5 text-sm text-bad">
            {error}
          </p>
        )}
        <div className="mt-6 flex flex-wrap items-baseline gap-x-5 gap-y-3">
          <Button type="submit" variant="secondary" disabled={busy}>
            {view.saved ? "Guardar cambios" : "Usar este formato"}
          </Button>
          {view.saved && (
            <Button variant="quiet" className="text-sm" onClick={clear} disabled={busy}>
              Volver al de siempre
            </Button>
          )}
        </div>
      </form>
    </section>
  );
}

function Row({
  label,
  count,
  points,
  onCount,
  onPoints,
}: {
  label: string;
  count: string;
  points: string;
  onCount: (v: string) => void;
  onPoints: (v: string) => void;
}) {
  const lower = label.toLocaleLowerCase("es");
  return (
    <>
      <span>{label}</span>
      <input
        type="text"
        inputMode="numeric"
        autoComplete="off"
        aria-label={`Cantidad de ${lower}`}
        value={count}
        onChange={(e) => onCount(e.target.value)}
        className={`w-full min-w-0 tabular-nums ${FIELD}`}
      />
      <input
        type="text"
        inputMode="decimal"
        autoComplete="off"
        aria-label={`Puntos cada uno de ${lower}`}
        value={points}
        onChange={(e) => onPoints(e.target.value)}
        className={`w-full min-w-0 tabular-nums ${FIELD}`}
      />
    </>
  );
}

function NumberField({
  label,
  value,
  onChange,
  inputMode = "decimal",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  inputMode?: "decimal" | "numeric";
}) {
  const id = useId();
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        type="text"
        inputMode={inputMode}
        autoComplete="off"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={`w-28 tabular-nums ${FIELD}`}
      />
    </div>
  );
}
