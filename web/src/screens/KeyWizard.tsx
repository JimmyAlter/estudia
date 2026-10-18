import { useRef, useState, type ClipboardEvent, type FormEvent } from "react";
import { ApiError, api } from "../api";
import { Button, Label } from "../ui";

export const AI_STUDIO_URL = "https://aistudio.google.com/apikey";

/** Solo una pista: Google puede cambiar el formato, así que nunca se bloquea el envío por esto. */
export const GEMINI_KEY_PATTERN = /^AIza[0-9A-Za-z_-]{35}$/;

// Mismos textos que src/estudia/ai/keys.py.
export const KEY_FORMAT_TEXT =
  "Eso no parece una clave de Gemini: empieza con AIza y tiene 39 caracteres. Revisá que se haya copiado entera.";
export const KEY_UNAVAILABLE_TEXT = "No se pudo verificar la clave ahora. Probá en un rato.";

const QUOTES = "\"'`“”„‘’‚«»";

/** Misma regla que `normalize_key` del servidor: saca espacios y comillas de los bordes. null si queda vacía. */
export function normalizeKey(raw: string): string | null {
  let key = raw.trim();
  while (key.length >= 2 && QUOTES.includes(key[0]) && QUOTES.includes(key[key.length - 1])) {
    key = key.slice(1, -1).trim();
  }
  let start = 0;
  let end = key.length;
  while (start < end && (QUOTES.includes(key[start]) || " \t\r\n".includes(key[start]))) start++;
  while (end > start && (QUOTES.includes(key[end - 1]) || " \t\r\n".includes(key[end - 1]))) end--;
  key = key.slice(start, end);
  return key || null;
}

function Heading({ children }: { children: string }) {
  return <h1 className="mb-3 font-serif text-[2rem] leading-tight tracking-[-0.02em]">{children}</h1>;
}

const LINK = "underline decoration-line underline-offset-4 hover:decoration-ink";

export default function KeyWizard({ rejected, onDone }: { rejected: boolean; onDone: () => void }) {
  const [key, setKey] = useState("");
  const [shown, setShown] = useState(false);
  const [busy, setBusy] = useState(false);
  /** Lo que respondió el servidor sobre la clave (tal cual): va con el campo. */
  const [fieldError, setFieldError] = useState<string | null>(null);
  /** Problema que no es de la clave (Google no respondió). */
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  // Pista tranquila mientras se escribe o pega. Nunca impide enviar.
  const normalized = normalizeKey(key);
  const hint = !fieldError && normalized && !GEMINI_KEY_PATTERN.test(normalized) ? KEY_FORMAT_TEXT : null;
  const message = fieldError ?? hint;

  async function verify(raw: string) {
    setError(null);
    const clean = normalizeKey(raw);
    // Vacía no llega acá: "Guardar clave" está apagado hasta que haya algo.
    if (!clean) return;
    setBusy(true);
    setFieldError(null);
    try {
      await api.setAiKey(clean);
      setKey("");
      onDone();
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        setFieldError(err.detail);
        input.current?.focus();
      } else {
        setError(KEY_UNAVAILABLE_TEXT);
      }
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void verify(key);
  }

  function paste(event: ClipboardEvent<HTMLInputElement>) {
    const text = event.clipboardData.getData("text");
    const clean = normalizeKey(text);
    if (!clean) return;
    event.preventDefault();
    setKey(clean);
    setFieldError(null);
    // Con el formato esperado se verifica sola; si no, queda la pista y se decide con "Guardar clave".
    if (GEMINI_KEY_PATTERN.test(clean) && !busy) void verify(clean);
  }

  return (
    <form onSubmit={submit}>
      <Heading>Conectá tu IA</Heading>
      <p className="mb-8 text-muted">
        {rejected
          ? "Google rechazó la clave guardada. Pegá una nueva para seguir procesando tus clases."
          : "Estudia usa Gemini, de Google, para armar resúmenes y preguntas con tus clases. La clave es gratis."}
      </p>
      <ol className="mb-8 list-decimal space-y-2 pl-5 marker:text-muted">
        <li>
          <a href={AI_STUDIO_URL} target="_blank" rel="noopener noreferrer" className={`${LINK} whitespace-nowrap`}>
            Abrí Google AI Studio
          </a>{" "}
          e iniciá sesión con tu cuenta de Google.
        </li>
        <li>Tocá «Create API key» (crear clave). Si te pregunta por un proyecto, elegí el que te propone.</li>
        <li>Copiá la clave, que empieza con AIza, y pegala acá abajo. Se verifica sola.</li>
      </ol>
      <div className="mb-2 flex items-baseline justify-between">
        <label htmlFor="ai-key" className="text-sm font-medium">
          Clave de Gemini
        </label>
        {/* Sin nada escrito no hay qué mostrar. */}
        {key && (
          <button
            type="button"
            onClick={() => setShown((s) => !s)}
            aria-pressed={shown}
            className="rounded-sm text-sm text-muted underline decoration-line underline-offset-4 hover:text-ink"
          >
            {shown ? "Ocultar" : "Mostrar"}
          </button>
        )}
      </div>
      <input
        id="ai-key"
        type={shown ? "text" : "password"}
        autoComplete="off"
        spellCheck={false}
        ref={input}
        value={key}
        onChange={(e) => {
          setKey(e.target.value);
          // El aviso era sobre lo que había antes: al corregir, se va.
          setFieldError(null);
        }}
        onPaste={paste}
        aria-invalid={fieldError ? true : undefined}
        aria-describedby={message ? "ai-key-message" : undefined}
        className="w-full rounded-md border border-field bg-canvas px-3 py-2.5 font-mono text-sm"
      />
      {message && (
        <p
          id="ai-key-message"
          role={fieldError ? "alert" : undefined}
          // Lo de Google va en tinta, sin rojo: el color queda para bien y mal en las respuestas.
          className={`mt-2 text-sm ${fieldError ? "text-ink" : "text-muted"}`}
        >
          {message}
        </p>
      )}
      <div className="mt-8 mb-8 space-y-1 text-sm text-muted">
        <Label className="mb-1">Qué ve Google</Label>
        <p>Google recibe el video de cada clase y las preguntas que Estudia arma. No recibe tus respuestas ni tus datos personales.</p>
        <p>En el plan gratuito, personas de Google pueden leer lo que se envía y usarlo para mejorar sus productos.</p>
      </div>
      <Button type="submit" disabled={busy || !normalized}>
        {busy ? "Verificando…" : "Guardar clave"}
      </Button>
      {/* Google no respondió: no es un error de la clave ni de quien la pegó, así que va sin rojo. */}
      {error && (
        <p role="alert" className="mt-4 text-sm text-ink">
          {error}
        </p>
      )}
    </form>
  );
}
