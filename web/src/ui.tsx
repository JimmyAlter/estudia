// Piezas mínimas compartidas por las pantallas.
import { useId, type ButtonHTMLAttributes, type ReactNode, type Ref } from "react";

type Variant = "primary" | "secondary" | "quiet";

const VARIANTS: Record<Variant, string> = {
  // Deshabilitado sin opacidad: el texto mantiene contraste de al menos 3:1.
  primary: "bg-ink text-canvas px-4 py-2 rounded-md font-medium hover:opacity-85 disabled:bg-muted disabled:hover:opacity-100",
  secondary: "border border-line px-4 py-2 rounded-md font-medium hover:bg-hover disabled:text-muted disabled:hover:bg-transparent",
  quiet:
    "text-muted underline underline-offset-4 decoration-line hover:text-ink disabled:text-faint focus-visible:outline-offset-[3px]",
};

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      className={`press cursor-pointer disabled:cursor-default ${VARIANTS[variant]} ${className}`}
      {...props}
    />
  );
}

/** Rótulo chico en versalitas, como en los bocetos ("Próximo examen"). */
export function Label({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`text-[11px] font-medium uppercase tracking-[0.08em] text-muted ${className}`}>
      {children}
    </div>
  );
}

/** Mensaje de una línea, sin caja. */
export function Notice({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted">{children}</p>;
}

/** Clases de un campo de texto o selector, como en el primer inicio. */
export const FIELD = "rounded-md border border-field bg-canvas px-3 py-2 aria-invalid:border-bad";

export const TYPED_HELP = "Escribí el resultado: 2√2, 3/4, 2*sqrt(2)…";

/**
 * Respuesta escrita (práctica y simulacro): el campo, la ayuda y, si hay, el error del campo.
 * Con `submitLabel` lleva su botón y Enter lo envía.
 */
export function TypedField({
  value,
  onChange,
  onSubmit,
  submitLabel,
  error,
  disabled,
  inputRef,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit?: () => void;
  submitLabel?: string;
  error?: string | null;
  disabled?: boolean;
  inputRef?: Ref<HTMLInputElement>;
}) {
  const id = useId();
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit?.();
      }}
    >
      <label htmlFor={`${id}-in`} className="mb-1.5 block text-sm font-medium">
        Tu respuesta
      </label>
      <div className="flex flex-wrap gap-3">
        <input
          id={`${id}-in`}
          ref={inputRef}
          type="text"
          inputMode="text"
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          maxLength={200}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={`${id}-help${error ? ` ${id}-err` : ""}`}
          className={`math min-w-0 flex-1 basis-56 text-[1.0625rem] ${FIELD}`}
        />
        {submitLabel && (
          <Button type="submit" variant="secondary" disabled={disabled || !value.trim()}>
            {submitLabel}
          </Button>
        )}
      </div>
      {error && (
        <p id={`${id}-err`} role="alert" className="mt-2 text-sm text-bad">
          {error}
        </p>
      )}
      <p id={`${id}-help`} className="mt-2 text-sm text-muted">
        {TYPED_HELP}
      </p>
    </form>
  );
}
