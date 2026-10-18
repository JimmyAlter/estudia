import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom no implementa el scroll de la ventana: sin esto cada cambio de pantalla deja un
// "Not implemented: window.scrollTo" en la salida. Los tests que lo miran le ponen un spy encima.
window.scrollTo = (() => {}) as typeof window.scrollTo;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
