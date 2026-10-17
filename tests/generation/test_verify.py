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
