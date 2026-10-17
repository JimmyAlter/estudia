import pytest

from estudia.generation.notation import sentence_case, student_math


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Casos reales de la auditoría.
        ("`f(x) = ax + b`", "f(x) = ax + b"),
        ("¿Cuál es la forma general de una función lineal `f: R -> R`?",
         "¿Cuál es la forma general de una función lineal f: R → R?"),
        ("y = x**2 + 3", "y = x² + 3"),
        ("(x - 3)**2", "(x − 3)²"),
        ("f(x) = ax**2 + bx + c", "f(x) = ax² + bx + c"),
        ("a*x0", "a·x0"),
        ("a1 * a2 = -1", "a1 · a2 = −1"),
        ("m != 0", "m ≠ 0"),
        # Potencias.
        ("x^2", "x²"),
        ("x^(2)", "x²"),
        ("x**(3)", "x³"),
        ("x**10", "x¹⁰"),
        ("x**-1", "x⁻¹"),
        ("x^(-12)", "x⁻¹²"),
        ("x ** 2", "x²"),
        ("x**n", "xⁿ"),
        ("x^n + 1", "xⁿ + 1"),
        ("x**(1/2)", "x^(1/2)"),
        ("2**3 = 8", "2³ = 8"),
        ("x**2.5", "x^2.5"),
        # Raíz, comparaciones y flechas.
        ("sqrt(2)", "√(2)"),
        ("2*sqrt(x + 1)", "2·√(x + 1)"),
        ("x <= 3 y x >= -1", "x ≤ 3 y x ≥ −1"),
        ("R -> R", "R → R"),
        # Producto.
        ("3*(x + 1)", "3·(x + 1)"),
        ("(x + 1)*(x - 1)", "(x + 1)·(x − 1)"),
        ("f(x)*g(x)", "f(x)·g(x)"),
        ("2*x*y", "2·x·y"),
        ("x**2*y", "x²·y"),
        ("5 * 3 = 15", "5 · 3 = 15"),
        # Menos.
        ("f(x) = -x + 2", "f(x) = −x + 2"),
        ("(-3, 2)", "(−3, 2)"),
        ("x-3", "x−3"),
        ("x - 3", "x − 3"),
        # Lo que no es matemática queda igual.
        ("x0", "x0"),
        ("teórico-práctico", "teórico-práctico"),
        ("COVID-19", "COVID-19"),
        ("del 10-12 de octubre", "del 10-12 de octubre"),
        ("Clase 1 - Funciones", "Clase 1 - Funciones"),
        ("Hay 3* respuestas", "Hay 3* respuestas"),
        ("Ojo*: esto no es un producto", "Ojo*: esto no es un producto"),
        ("la variable *x* y la *y*", "la variable *x* y la *y*"),
        ("Esto es *importante* para el parcial", "Esto es *importante* para el parcial"),
        ("```python\nx = 1\n```", "```python\nx = 1\n```"),
        ("", ""),
    ],
)
def test_student_math(raw, expected):
    assert student_math(raw) == expected


def test_markdown_bold_in_summaries_is_kept():
    text = "**Definición**: una función lineal es **f(x) = mx + b**. Hay **2 casos**."
    assert student_math(text) == text


def test_bold_with_powers_inside_and_outside():
    assert (student_math("**Cuadrática**: y = x**2 + 2**x")
            == "**Cuadrática**: y = x² + 2^x")
    assert student_math("**Ejemplo: x**2**") == "**Ejemplo: x²**"


def test_summary_bullets_and_lines_are_kept():
    text = "# Rectas\n\n* La pendiente es m\n* y = 2*x + 1\n- Si x = 0, y = 1\n---"
    assert student_math(text) == (
        "# Rectas\n\n* La pendiente es m\n* y = 2·x + 1\n- Si x = 0, y = 1\n---"
    )


def test_line_break_is_not_a_product():
    assert student_math("y = 2\n* a es la pendiente") == "y = 2\n* a es la pendiente"


@pytest.mark.parametrize(
    "raw",
    [
        "y = x**2 + 3", "(x - 3)**2", "a1 * a2 = -1", "x**(1/2)", "`f: R -> R`",
        "**Definición**: f(x) = ax**2 + bx + c", "x^(-12)", "2*sqrt(x + 1)",
    ],
)
def test_student_math_is_idempotent(raw):
    once = student_math(raw)
    assert student_math(once) == once


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Definición y Gráfica de la Función Cuadrática",
         "Definición y gráfica de la función cuadrática"),
        ("Funciones Lineales", "Funciones lineales"),
        ("funciones lineales", "Funciones lineales"),
        ("Ingreso a la UTN", "Ingreso a la UTN"),
        ("Álgebra En La FRBA", "Álgebra en la FRBA"),
        ("Funciones De R En R", "Funciones de R en R"),
        ("Rectas Y Planos", "Rectas y planos"),
        ("Teorema de Pitágoras y sus aplicaciones", "Teorema de Pitágoras y sus aplicaciones"),
        ("Serie de McLaurin", "Serie de McLaurin"),
        ("Función Lineal-Afín (Parte II)", "Función lineal-afín (parte II)"),
        ("  Límites   Laterales ", "Límites laterales"),
        ("¿Qué Es Una Función?", "¿Qué es una función?"),
        ("", ""),
    ],
)
def test_sentence_case(raw, expected):
    assert sentence_case(raw) == expected


def test_sentence_case_is_idempotent():
    for raw in ("Definición y Gráfica de la Función Cuadrática", "Ingreso a la UTN"):
        once = sentence_case(raw)
        assert sentence_case(once) == once
