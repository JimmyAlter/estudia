// Formatos de texto para la interfaz (fechas, duraciones, nombres del campus).

const TIMEZONE = "America/Argentina/Buenos_Aires";

/**
 * "PRIMER CUESTIONARIO" → "Primer cuestionario". Solo toca textos enteros en mayúsculas y con
 * alguna palabra larga: las siglas cortas ("TP 1", "AM1") quedan como están.
 */
export function tidy(text: string): string {
  const trimmed = text.trim().replace(/\.$/, "");
  const letters = trimmed.replace(/[^\p{L}]/gu, "");
  if (letters.length < 2 || letters !== letters.toLocaleUpperCase("es")) return trimmed;
  if (!/\p{L}{4,}/u.test(trimmed)) return trimmed;
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

/** 1773489600 → "14/3" (día y mes en Buenos Aires). */
export function shortDate(epochSeconds: number): string {
  return new Intl.DateTimeFormat("es-AR", { day: "numeric", month: "numeric", timeZone: TIMEZONE }).format(
    new Date(epochSeconds * 1000),
  );
}

/** Hora local de un instante: "8:30", "19:00". */
export function clock(epochSeconds: number): string {
  return new Intl.DateTimeFormat("es-AR", {
    hour: "numeric",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: TIMEZONE,
  })
    .format(new Date(epochSeconds * 1000))
    .replace(/^0(?=\d)/, "");
}

/** Minutos desde la medianoche → "8:30". */
export function minutesToClock(minutes: number): string {
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}`;
}

/** Minutos desde la medianoche → "08:30" (valor de un `<input type="time">`). */
export function minutesToInput(minutes: number): string {
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

/** "08:30" → 510; null si no es una hora. */
export function inputToMinutes(value: string): number | null {
  const match = value.match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return null;
  const minutes = Number(match[1]) * 60 + Number(match[2]);
  return minutes < 1440 ? minutes : null;
}

/** Día local de un instante como "2026-10-13" (la clave de los días de la agenda). */
export function dayKey(epochSeconds: number): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: TIMEZONE }).format(new Date(epochSeconds * 1000));
}

/** "2026-11-20" y "18:00" en Buenos Aires (UTC−3, sin horario de verano) → epoch en segundos. */
export function localToEpoch(date: string, time: string): number | null {
  const ms = Date.parse(`${date}T${time}:00-03:00`);
  return Number.isNaN(ms) ? null : Math.round(ms / 1000);
}

/** "2026-10-13" → "martes 13". */
export function weekdayAndDay(key: string): string {
  const date = new Date(`${key}T12:00:00-03:00`);
  return new Intl.DateTimeFormat("es-AR", { weekday: "long", day: "numeric", timeZone: TIMEZONE })
    .format(date)
    .replace(",", "");
}

/** Cuánto tiempo, en redondo: "unos 40 min", "una hora", "unas 6 h". */
export function aboutTime(minutes: number): string {
  if (minutes < 60) return `unos ${Math.max(5, Math.round(minutes / 5) * 5)} min`;
  const hours = Math.round(minutes / 60);
  return hours === 1 ? "una hora" : `unas ${hours} h`;
}

/** Tiempo libre: "40 min", "1 h", "4 h". */
export function freeTime(minutes: number): string {
  return minutes < 60 ? `${Math.round(minutes)} min` : `${Math.round(minutes / 60)} h`;
}

/** Nota en la escala del examen, con coma: 7.5 → "7,5". */
export function grade(value: number): string {
  return new Intl.NumberFormat("es-AR", { maximumFractionDigits: 2 }).format(value);
}
