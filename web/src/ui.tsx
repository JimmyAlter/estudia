// Piezas mínimas compartidas por las pantallas.
import type { ButtonHTMLAttributes, ReactNode } from "react";

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
