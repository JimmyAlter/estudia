import { useEffect, useState } from "react";
import { api, type ProgressData } from "../api";
import { grade, tidy } from "../format";
import { Label, Notice } from "../ui";

/** "Cómo vengo": cuánto acertás según lo seguro que decís estar, y la nota prevista contra la real. */
export default function Progress() {
  const [data, setData] = useState<ProgressData | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .progress()
      .then((p) => alive && setData(p))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, []);

  if (failed) return <Notice>No se pudo cargar. Probá recargar la página.</Notice>;
  if (!data) return null;
  const { calibration, predictions, gap } = data;

  return (
    <div className="max-w-xl">
      <h1 className="mb-12 font-serif text-[2rem] leading-tight tracking-[-0.02em]">Cómo vengo</h1>

      <section className="mb-14">
        <h2 className="mb-1 font-semibold">Tu confianza</h2>
        <p className="mb-5 text-sm text-muted">Lo que decís antes de ver la corrección, contra lo que pasó.</p>
        {calibration.missing > 0 ? (
          <p className="text-muted">
            {calibration.missing === 1
              ? "Falta 1 respuesta con confianza para calcularlo."
              : `Faltan ${calibration.missing} respuestas con confianza para calcularlo.`}
          </p>
        ) : (
          <ul>
            {calibration.levels.map((level) => {
              const percent = Math.round(level.hit_rate * 100);
              return (
                <li key={level.confidence} className="border-b border-line py-3">
                  <span className="flex flex-wrap items-baseline justify-between gap-x-6">
                    <span>{`Cuando decís «${level.label}», acertás ${percent} %`}</span>
                    <span className="text-sm tabular-nums text-muted">
                      {level.answers === 1 ? "1 respuesta" : `${level.answers} respuestas`}
                    </span>
                  </span>
                  <span className="mt-2 block h-1 overflow-hidden rounded-full bg-track" aria-hidden>
                    <span className="block h-full rounded-full bg-ink/75" style={{ width: `${percent}%` }} />
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section>
        <h2 className="mb-1 font-semibold">Tu nota prevista</h2>
        {predictions.length ? (
          <>
            <ul className="mt-4">
              {predictions.map((p) => (
                <li key={p.exam_id} className="border-b border-line py-3">
                  <Label className="mb-1">{tidy(p.exam)}</Label>
                  <p className="tabular-nums">
                    {p.predicted != null
                      ? `Esperabas ${grade(p.predicted)}, el simulacro dio ${grade(p.mock_grade)}, sacaste ${grade(p.real)}.`
                      : `El simulacro dio ${grade(p.mock_grade)}, sacaste ${grade(p.real)}.`}
                  </p>
                </li>
              ))}
            </ul>
            {/* Con una sola predicción, el promedio repetiría la misma cuenta. */}
            {gap != null && predictions.filter((p) => p.predicted != null).length >= 2 && (
              <p className="mt-5 tabular-nums">
                {`En promedio le errás por ${grade(gap)} ${gap === 1 ? "punto" : "puntos"} sobre 10.`}
              </p>
            )}
          </>
        ) : (
          <p className="text-sm text-muted">
            Cuando cargues la nota de un examen en el que hiciste un simulacro, acá ves cuánto se pareció a lo que esperabas.
          </p>
        )}
      </section>
    </div>
  );
}
