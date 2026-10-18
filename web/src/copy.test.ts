import { describe, expect, it } from "vitest";

// Todo el código de la interfaz (sin los tests, que nombran las palabras para prohibirlas).
const sources = import.meta.glob<string>("./**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true });

describe("textos que no castigan", () => {
  it("ningún texto habla de fracaso ni de racha rota", () => {
    const files = Object.entries(sources).filter(([path]) => !/\.test\.[^/]+$/.test(path));
    expect(files.length).toBeGreaterThan(10);
    const offending = files.filter(([, text]) => /fracas|racha rota/i.test(text)).map(([path]) => path);
    expect(offending).toEqual([]);
  });
});
