import { useState, type ReactNode } from "react";
import { api, type Age } from "../api";
import { Button } from "../ui";

/** Guarda la respuesta de edad; devuelve si salió bien. */
function useAnswer(onDone: () => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function answer(age: Age) {
    setBusy(true);
    setError(null);
    try {
      await api.saveSettings({ age });
      onDone();
    } catch {
      setError("No se pudo guardar. Probá de nuevo.");
      setBusy(false);
    }
  }
  return { busy, error, answer };
}

/** Paso del primer inicio: va antes de pedir la clave de Gemini. */
export function AgeStep({ onDone, heading }: { onDone: () => void; heading: ReactNode }) {
  const { busy, error, answer } = useAnswer(onDone);
  return (
    <div>
      {heading}
      <p className="mb-8 text-muted">
        La IA de Estudia usa Gemini, de Google, que pide tener 18 años o más. Si no los tenés, igual podés usar la agenda,
        el plan, los recordatorios y el calendario; solo queda sin IA.
      </p>
      <div className="flex gap-3">
        <Button disabled={busy} onClick={() => answer("adult")}>
          Sí
        </Button>
        <Button variant="secondary" disabled={busy} onClick={() => answer("minor")}>
          No
        </Button>
      </div>
      {error && (
        <p role="alert" className="mt-4 text-sm text-bad">
          {error}
        </p>
      )}
    </div>
  );
}

/** Instalaciones anteriores a la pregunta: una línea tranquila arriba de Inicio. */
export function AgeNotice({ onDone }: { onDone: () => void }) {
  const { busy, error, answer } = useAnswer(onDone);
  return (
    <div className="border-t border-line pt-3 text-sm text-muted">
      <p>
        ¿Tenés 18 años o más? Hasta que contestes, Estudia no prepara material nuevo con IA.{" "}
        <Button variant="quiet" disabled={busy} onClick={() => answer("adult")}>
          Sí
        </Button>
        <span aria-hidden> · </span>
        <Button variant="quiet" disabled={busy} onClick={() => answer("minor")}>
          No
        </Button>
        {error && (
          <span role="status" className="ml-2 text-bad">
            {error}
          </span>
        )}
      </p>
    </div>
  );
}
