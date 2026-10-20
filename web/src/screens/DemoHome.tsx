import { useState } from "react";
import { api } from "../api";
import { Button } from "../ui";

interface Props {
  onPractice: () => void;
  onShortMock: (examId: number) => void;
}

/** El inicio de la materia de ejemplo: practicar y un simulacro corto. Nada más. */
export default function DemoHome({ onPractice, onShortMock }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // El examen se lee al tocar: el inicio aparece entero de una, sin nada que llegue después.
  async function shortMock() {
    setBusy(true);
    setError(null);
    try {
      // Una inscripción no es un examen: no tiene simulacro.
      const exam = (await api.exams()).find((e) => e.kind !== "inscripcion");
      if (!exam) throw new Error("sin examen");
      onShortMock(exam.id);
    } catch {
      setError("No se pudo abrir el simulacro. Probá de nuevo.");
      setBusy(false);
    }
  }

  return (
    <div className="max-w-xl">
      <h1 className="font-serif text-[2.6rem] leading-[1.1] tracking-[-0.025em]">Matemática de ejemplo</h1>
      <p className="mt-3 text-muted">2 temas y 12 preguntas, escritas para probar Estudia.</p>

      <div className="mt-10 flex flex-wrap gap-3">
        <Button onClick={onPractice}>Practicar</Button>
        <Button variant="secondary" onClick={shortMock} disabled={busy}>
          Hacer un simulacro corto
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
