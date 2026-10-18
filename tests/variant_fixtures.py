"""Pregunta con plantilla de variantes que usan los tests de las variantes (etapa 3c)."""
from estudia.ai.base import GenQuestion, VariantParam, VariantTemplate


def linear_template() -> VariantTemplate:
    return VariantTemplate(
        params=[
            VariantParam(name="a", low=2, high=9),
            VariantParam(name="b", low=1, high=9),
            VariantParam(name="c", low=2, high=9),
        ],
        original=[2, 3, 4],
        stem="Si f(x) = {a}x + {b}, ¿cuánto vale f({c})?",
        answer_expr="a*c + b",
        distractor_exprs=["a*(c + b)", "a + b", "a*c"],
        explanation="f({c}) = {a}·{c} + {b} = {respuesta}.",
        common_error="Olvidarse de sumar {b} a {a}·{c}.",
    )


def linear_question() -> GenQuestion:
    return GenQuestion(
        stem="Si f(x) = 2x + 3, ¿cuánto vale f(4)?",
        options=["11", "14", "5", "8"],
        correct_index=0,
        explanation="f(4) = 2·4 + 3 = 11.",
        common_error="Olvidarse de sumar 3 a 2·4.",
        topic="Funciones lineales",
        source_seconds=60,
        check_expr="11",
        variant=linear_template(),
    )


def variant_payload(question, seed: str | None = None, limit: int = 4) -> dict:
    """El payload de `verify.compute_variant_values` que arma la tubería, desde una pregunta."""
    from estudia.generation.variants import variant_payload as build

    payload = build(question, limit)
    if seed is not None:
        payload["seed"] = seed
    return payload


def simple_question(stem: str, template_stem: str, options: list[str], check_expr: str,
                    params: list[tuple[str, int, int]], original: list[int], answer_expr: str,
                    distractor_exprs: list[str], derived: tuple = (),
                    explanation: str = "Da {respuesta}.", common_error: str = "Error.") -> GenQuestion:
    """Una pregunta con plantilla corta: la explicación original es la plantilla con la clave.

    La plantilla se lee de un dict, como el JSON del modelo: un `derived` que mande igual se
    pasa tal cual (las plantillas ya no lo tienen y lo ignoran).
    """
    template = {
        "params": [{"name": n, "low": lo, "high": hi} for n, lo, hi in params],
        "original": original, "stem": template_stem, "answer_expr": answer_expr,
        "distractor_exprs": distractor_exprs, "explanation": explanation, "common_error": common_error,
    }
    if derived:
        template["derived"] = [{"name": n, "expr": e} for n, e in derived]
    return GenQuestion(
        stem=stem, options=options, correct_index=0,
        explanation=explanation.replace("{respuesta}", options[0]),
        common_error=common_error.replace("{respuesta}", options[0]),
        topic="Funciones lineales", source_seconds=0, check_expr=check_expr,
        variant=VariantTemplate.model_validate(template))


def percent_question() -> GenQuestion:
    """Otra pregunta con plantilla válida (de las sondas que se portaron bien en la Tarea 6)."""
    q = simple_question("¿Cuánto es el 20% de 50?", "¿Cuánto es el {a}% de 50?", ["10", "20", "5", "40"],
                        "10", [("a", 11, 30)], [20], "a/2", ["a", "a/4", "2*a"])
    return q.model_copy(update={"topic": "Porcentajes"})
