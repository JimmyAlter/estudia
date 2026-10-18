import { describe, expect, it } from "vitest";
import { keepMath, tidy } from "./format";

const NBSP = " ";

describe("keepMath", () => {
  it("mantiene juntas las cuentas sin tocar la prosa", () => {
    expect(keepMath("la recta y = kx + 2 es paralela")).toBe(
      `la recta y${NBSP}=${NBSP}kx${NBSP}+${NBSP}2 es paralela`,
    );
    expect(keepMath("pasa por (2, 1) y sigue")).toBe(`pasa por (2,${NBSP}1) y sigue`);
    expect(keepMath("m₁ · m₂ = −1")).toBe(`m₁${NBSP}·${NBSP}m₂${NBSP}=${NBSP}−1`);
  });
});

describe("tidy", () => {
  it("baja las mayúsculas de un título, pero no las de una sigla corta", () => {
    expect(tidy("PRIMER CUESTIONARIO.")).toBe("Primer cuestionario");
    expect(tidy("TP 1")).toBe("TP 1");
    expect(tidy("AM1")).toBe("AM1");
  });
});
