// Formatos de texto para la interfaz (fechas, duraciones, nombres del campus).

const TIMEZONE = "America/Argentina/Buenos_Aires";

/** "PRIMER CUESTIONARIO" → "Primer cuestionario". Solo toca textos enteros en mayúsculas. */
export function tidy(text: string): string {
  const trimmed = text.trim().replace(/\.$/, "");
  const letters = trimmed.replace(/[^\p{L}]/gu, "");
  if (letters.length < 2 || letters !== letters.toLocaleUpperCase("es")) return trimmed;
  const lower = trimmed.toLocaleLowerCase("es");
  return lower.charAt(0).toLocaleUpperCase("es") + lower.slice(1);
}

/** 1773489600 → "sábado 14 de marzo a las 9:00". */
export function formatDate(epochSeconds: number): string {
  const date = new Date(epochSeconds * 1000);
  const day = new Intl.DateTimeFormat("es-AR", {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: TIMEZONE,
  }).format(date);
  const time = new Intl.DateTimeFormat("es-AR", {
    hour: "numeric",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: TIMEZONE,
  }).format(date);
  return `${day.replace(",", "")} a las ${time}`;
}

/** 7200 → "2 horas"; 5400 → "1 h 30 min"; 1800 → "30 minutos". */
export function formatDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  if (hours && minutes) return `${hours} h ${minutes} min`;
  if (hours) return hours === 1 ? "1 hora" : `${hours} horas`;
  return minutes === 1 ? "1 minuto" : `${minutes} minutos`;
}

/** 1060 → "17:40"; 3725 → "1:02:05". */
export function formatTimestamp(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** Hace cuánto, en palabras: "recién", "hace 5 min", "hace 2 h", "hace 3 días". */
export function timeAgo(epochSeconds: number, now = Date.now() / 1000): string {
  const diff = Math.max(0, now - epochSeconds);
  if (diff < 60) return "recién";
  if (diff < 3600) return `hace ${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `hace ${Math.floor(diff / 3600)} h`;
  const days = Math.floor(diff / 86400);
  return days === 1 ? "ayer" : `hace ${days} días`;
}

export function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

const OPERATOR = "[=+\\-−·/×<>≤≥]";
const AROUND_OPERATOR = new RegExp(` (?=${OPERATOR})|(?<=${OPERATOR}) `, "g");

/**
 * Evita que una cuenta se corte a mitad de línea ("y = kx + 2" queda junta):
 * los espacios alrededor de operadores y dentro de paréntesis pasan a ser
 * espacios duros. El resto del texto se corta normalmente.
 */
export function keepMath(text: string): string {
  return text
    .replace(AROUND_OPERATOR, "\u00a0")
    .replace(/\([^()]*\)/g, (group) => group.replace(/ /g, "\u00a0"));
}
