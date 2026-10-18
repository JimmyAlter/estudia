import { describe, expect, it } from "vitest";
import { grade, keepMath, tidy } from "./format";

const NBSP = " ";

describe("keepMath", () => {
  it("mantiene juntas las cuentas sin tocar la prosa", () => {
    expect(keepMath("la recta y = kx + 2 es paralela")).toBe(
      `la recta y${NBSP}= kx${NBSP}+${NBSP}2 es paralela`,
    );
    expect(keepMath("pasa por (2, 1) y sigue")).toBe(`pasa por (2,${NBSP}1) y sigue`);
    expect(keepMath("m₁ · m₂ = −1")).toBe(`m₁${NBSP}·${NBSP}m₂${NBSP}= −1`);
  });

  it("una cadena larga de igualdades solo se corta después de un '='", () => {
    const out = keepMath("d = √((x₂ − x₁)² + (y₂ − y₁)²) = √(9 + 16) = √25 = 5");
    const breaks = [...out.matchAll(/ /g)].map((m) => m.index ?? 0);
    expect(breaks.length).toBe(4);
    for (const i of breaks) expect(out[i - 1]).toBe("=");
    // ningún "=" queda pegado al principio de una línea nueva
    expect(out).not.toMatch(/ =/);
  });
});

describe("tidy", () => {
  it("baja las mayúsculas de un título, pero no las de una sigla corta", () => {
    expect(tidy("PRIMER CUESTIONARIO.")).toBe("Primer cuestionario");
    expect(tidy("TP 1")).toBe("TP 1");
    expect(tidy("AM1")).toBe("AM1");
  });
});

describe("grade", () => {
  it("muestra la nota con un decimal como mucho", () => {
    expect(grade(20 / 3)).toBe("6,7");
    expect(grade(6.5)).toBe("6,5");
    expect(grade(8)).toBe("8");
  });
});
