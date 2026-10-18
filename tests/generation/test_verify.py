import time

import pytest

from estudia.generation.verify import check_math, check_math_tristate, parse_option


def test_resta_con_signo_menos_unicode():
    assert check_math("1-3*2", "−5") is True


def test_valor_distinto():
    assert check_math("1-3*2", "7") is False


def test_raiz():
    assert check_math("sqrt(8)", "2√2") is True


def test_coma_decimal():
    assert check_math("1/2", "0,5") is True


def test_decimal_exacto():
    assert check_math("1/10", "0,1") is True


def test_rejects_code():
    assert check_math("__import__('os').system('x')", "0") is False


@pytest.mark.parametrize(
    "expr",
    ["(lambda: 1)()", "().__class__", "1.real", "open('/etc/passwd')", "x", "2*__"],
)
def test_rechaza_expresiones_peligrosas(expr):
    assert check_math(expr, "0") is False


def test_opcion_no_numerica():
    assert check_math("2", "la pendiente") is False


def test_expr_vacia_o_larga():
    assert check_math("", "0") is False
    assert check_math("1+" * 150 + "1", "151") is False


def test_potencias_patologicas_son_rapidas():
    t0 = time.monotonic()
    assert check_math("9**9**9**9", "0") is False
    assert check_math("9**9**9", "0") is False
    assert check_math("9^9^9", "0") is False
    assert check_math("2**1000", "0") is False
    assert check_math("9**99**99", "0") is False
    assert check_math("2**(9)**99", "0") is False
    assert check_math("99**(99*99*99*99*99)", "0") is False
    assert check_math("(sqrt(2)+sqrt(3)+sqrt(5)+sqrt(7)+sqrt(11))**99", "2√3") is False
    assert time.monotonic() - t0 < 3


def test_potencias_chicas_funcionan():
    assert check_math("2**10", "1024") is True
    assert check_math("2^3", "8") is True
    assert check_math("2**(-1)", "1/2") is True


def test_parse_option_formatos():
    assert parse_option("−5") == -5
    assert parse_option("-5") == -5
    assert parse_option("1/2") == parse_option("0,5")
    assert parse_option("√2") == parse_option("sqrt(2)")
    assert parse_option("2√3") == parse_option("2*sqrt(3)")
    assert parse_option("la pendiente") is None
    assert parse_option("1,2,3") is None


def test_nunca_lanza():
    assert check_math("1/0", "0") is False
    assert check_math("1+", "0") is False
    assert check_math("1", "") is False


def test_timeout_devuelve_false_y_mata_el_worker(monkeypatch):
    import multiprocessing

    from estudia.generation import verify

    monkeypatch.setattr(verify, "EVAL_TIMEOUT_S", 0)
    assert check_math("1+1", "2") is False
    assert multiprocessing.active_children() == []


def test_camino_spawn_como_en_windows(monkeypatch):
    from estudia.generation import verify

    verify._stop_worker()
    monkeypatch.setattr(verify, "_start_method", lambda: "spawn")
    try:
        assert check_math("1-3*2", "−5") is True
        radical = "(sqrt(2)+sqrt(3)+sqrt(5)+sqrt(7)+sqrt(11))**99"
        assert check_math(radical, "2√3") is False
    finally:
        verify._stop_worker()


def test_sin_workers_al_terminar():
    import multiprocessing

    from estudia.generation import verify

    assert check_math("1+1", "2") is True
    verify._stop_worker()
    assert multiprocessing.active_children() == []


def test_interrupcion_no_deja_respuesta_vieja(monkeypatch):
    from estudia.generation import verify

    assert check_math("1+1", "2") is True  # asegura worker vivo
    real_conn = verify._conn

    class Interrumpida:
        def send(self, msg):
            real_conn.send(msg)

        def poll(self, timeout=None):
            raise KeyboardInterrupt

        def close(self):
            real_conn.close()

    monkeypatch.setattr(verify, "_conn", Interrumpida())
    with pytest.raises(KeyboardInterrupt):
        check_math("1+1", "2")
    monkeypatch.undo()
    assert check_math("1+1", "3") is False
    assert check_math("1+1", "2") is True


def test_worker_muerto_entre_llamadas_no_da_falso():
    from estudia.generation import verify

    assert check_math("1+1", "2") is True
    verify._proc.kill()
    verify._proc.join()
    assert check_math("1+1", "2") is True


def test_tristate_distingue_igual_distinto_y_no_se_sabe():
    assert check_math_tristate("1/2", "0,5") is True
    assert check_math_tristate("1/2", "0,25") is False
    # Una opción que no se puede leer puede ser equivalente: no se sabe.
    for option in ["no tiene solución", "½", "50%", "x = 1/2", "1/2 cm", "1/2 aprox."]:
        assert check_math_tristate("1/2", option) is None, option
        assert check_math("1/2", option) is False, option
    # La expresión de control mala no prueba nada.
    assert check_math_tristate("1+", "2") is None
    assert check_math_tristate("x+1", "2") is None
    assert check_math_tristate("1/0", "0") is None
    assert check_math_tristate(None, "2") is None


def test_tristate_timeout_es_none(monkeypatch):
    from estudia.generation import verify

    monkeypatch.setattr(verify, "EVAL_TIMEOUT_S", 0)
    assert check_math_tristate("1+1", "2") is None
    assert check_math_tristate("1+1", "3") is None


def test_tristate_error_del_worker_es_none(monkeypatch):
    from estudia.generation import verify

    monkeypatch.setattr(verify, "_start_worker", lambda: False)
    verify._stop_worker()
    assert check_math_tristate("1+1", "2") is None
    assert check_math("1+1", "2") is False


# --- Respuesta escrita (práctica) --------------------------------------------------------------

@pytest.mark.parametrize("exact, typed", [
    ("sqrt(2)", "1.41"), ("2*sqrt(2)", "2.83"), ("2*sqrt(2)", "2.828"), ("1/3", "0.333"),
    ("pi", "3.14"), ("1000/3", "333.3"), ("1/2", "0.5"),
    # formas exactas equivalentes
    ("2*sqrt(2)", "2√2"), ("2*sqrt(2)", "2*sqrt(2)"), ("2*sqrt(2)", "2 raiz 2"),
    ("2*sqrt(2)", "2 raíz de 2"), ("2*sqrt(2)", "√8"), ("2*sqrt(2)", "2,828"),
    ("-5", "−5"), ("1/2", "0,5"), ("1/4", "0.25"), ("25/2", "12.5"), ("9", "3²"), ("100", "１００"),
    ("1/2", "0.50"),
    # exponentes con superíndices
    ("1/2", "2⁻¹"), ("16", "2⁴"), ("1/1000", "10⁻³"), ("1000", "10³"),
    # decimal que empieza con punto y signo separado
    ("1/3", ".333"), ("-1/2", "-.5"), ("-sqrt(2)", "- 1.41"), ("-sqrt(2)", "−  1.41"),
])
def test_typed_accepted(exact, typed):
    from estudia.generation.verify import grade_typed

    assert grade_typed(exact, typed) is True


@pytest.mark.parametrize("exact, typed", [
    ("1/3", "0.3"), ("1/3", "0.33"), ("sqrt(2)", "1.4"), ("sqrt(2)", "1.42"), ("pi", "3.15"),
    ("1000/3", "333.4"), ("100", "99.99"), ("100", "99.9"), ("100", "100.1"), ("3", "2.99"),
    ("2*sqrt(2)", "2.82"), ("10", "9.99"),
    # casi iguales sin ser iguales: hace falta prueba simbólica
    ("1", "1+10^(-30)"), ("sqrt(2)", "sqrt(2)+10^(-40)"),
    ("2*sqrt(2)", "3"), ("2*sqrt(2)", "sqrt(2)"), ("9", "32"),
    # superíndices: no se pegan como dígitos sueltos
    ("1", "2⁻¹"), ("24", "2⁴"), ("21", "2⁻¹"),
    # exacto con desarrollo finito: solo vale igual, con los decimales que sean
    ("12345.678", "12345.68"), ("1/8", "0.13"), ("12345.678", "12345.7"),
    ("1/3", ".33"),
    # racional que queda sin evaluar: igual de exacto
    ("12345.678*(sqrt(2)+1)*(sqrt(2)-1)", "12345.68"),
])
def test_typed_rejected(exact, typed):
    from estudia.generation.verify import grade_typed

    assert grade_typed(exact, typed) is False


def test_typed_ilegible_es_none():
    from estudia.generation.verify import grade_typed

    for typed in ["", "   ", "dos", "x+1", "2√", "import os", "1/2 cm", "½", None,
                  "2ⁿ", "H₂", "2₁", "x²", "2⁻", "2^⁽1⁾"]:
        assert grade_typed("2", typed) is None, typed


def test_typed_control_ilegible_no_es_falla_del_worker():
    from estudia.generation import verify

    for check_expr in ["1/0", "x+1", "import os", None]:
        with pytest.raises(verify.UncheckableExpr):
            verify.grade_typed(check_expr, "2")


def test_typed_worker_caido_o_lento_no_es_ilegible(monkeypatch):
    from estudia.generation import verify

    monkeypatch.setattr(verify, "EVAL_TIMEOUT_S", 0)
    with pytest.raises(verify.GradingUnavailable):
        verify.grade_typed("2", "2")
    monkeypatch.undo()
    monkeypatch.setattr(verify, "_start_worker", lambda: False)
    verify._stop_worker()
    with pytest.raises(verify.GradingUnavailable):
        verify.grade_typed("2", "2")


# --- ¿La regla escrita acepta un distractor? (contradicción opción múltiple / escrita) ------

@pytest.mark.parametrize("check_expr, option, expected", [
    ("sqrt(2)", "1.41", True),       # la regla escrita lo acepta: contradice la opción múltiple
    ("sqrt(2)", "1,41", True),
    ("sqrt(2)", "√2", True),
    ("sqrt(2)", "2", False),
    ("sqrt(2)", "1.4", False),
    ("sqrt(2)", "hola", False),      # ilegible como respuesta escrita: no se acepta
])
def test_typed_accepts_option(check_expr, option, expected):
    from estudia.generation.verify import typed_accepts_option

    assert typed_accepts_option(check_expr, option) is expected


def test_typed_accepts_option_runs_in_the_worker_and_timeout_is_none(monkeypatch):
    from estudia.generation import verify

    seen = []
    real = verify._equal_with_timeout

    def spy(expected, given, places="option"):
        seen.append(places)
        return real(expected, given, places)

    monkeypatch.setattr(verify, "_equal_with_timeout", spy)
    assert verify.typed_accepts_option("sqrt(2)", "1.41") is True
    assert seen == [verify.TYPED_OPTION]
    monkeypatch.setattr(verify, "EVAL_TIMEOUT_S", 0)
    assert verify.typed_accepts_option("sqrt(2)", "1.41") is None


def test_typed_accepts_option_with_unusable_control_is_none():
    from estudia.generation.verify import typed_accepts_option

    assert typed_accepts_option("x+1", "1") is None
    assert typed_accepts_option(None, "1") is None


def test_typed_accepts_option_backs_off_after_a_failed_start(monkeypatch):
    from estudia.generation import verify

    starts = []

    def failing_start():
        starts.append(1)
        return False

    verify._stop_worker()
    monkeypatch.setattr(verify, "_start_worker", failing_start)
    assert verify.typed_accepts_option("2", "3") is None
    assert verify.typed_accepts_option("2", "3") is None
    assert len(starts) == 1
    # Corregir una respuesta de verdad no espera: vuelve a probar.
    with pytest.raises(verify.GradingUnavailable):
        verify.grade_typed("2", "2")
    assert len(starts) == 2
    # Pasada la espera se prueba de nuevo.
    monkeypatch.setattr(verify, "_unavailable_until", 0.0)
    assert verify.typed_accepts_option("2", "3") is None
    assert len(starts) == 3
