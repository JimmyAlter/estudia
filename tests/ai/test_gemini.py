import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors, types

from estudia.ai.base import (
    AIAuthError,
    AIFormatError,
    AIQuotaError,
    AIUnavailableError,
    AIVideoUnavailableError,
    VideoAnalysis,
)
from estudia.ai.blind import BlindAnswers
from estudia.ai.gemini import GeminiProvider
from estudia.material.video import VideoDownloadError


def _question(**over):
    q = {
        "stem": "Si f(x) = 2x + 1, cuánto vale f(3)?",
        "options": ["5", "6", "7", "8"],
        "correct_index": 2,
        "explanation": "2*3+1 = 7",
        "common_error": "Olvidar sumar 1",
        "topic": "Funciones",
        "source_seconds": 120,
        "check_expr": "7",
    }
    q.update(over)
    return q


def _analysis(**over):
    a = {"summary_md": "f(x) = ax + b", "topics": ["Funciones"], "questions": [_question()]}
    a.update(over)
    return a


class FakeFiles:
    """API de archivos falsa: el archivo subido pasa a ACTIVE después de `polls` consultas."""

    def __init__(self, polls=1, upload_error=None, final_state=types.FileState.ACTIVE):
        self.polls, self.upload_error, self.final_state = polls, upload_error, final_state
        self.uploaded, self.deleted, self.gets = [], [], 0

    def upload(self, file, config):
        if self.upload_error:
            raise self.upload_error
        self.uploaded.append((Path(file), config["mime_type"]))
        state = self.final_state if self.polls == 0 else types.FileState.PROCESSING
        return types.File(name="files/abc", uri="https://gemini/files/abc",
                          mime_type=config["mime_type"], state=state)

    def get(self, name):
        self.gets += 1
        state = self.final_state if self.gets >= self.polls else types.FileState.PROCESSING
        return types.File(name=name, uri="https://gemini/files/abc", mime_type="video/mp4", state=state)

    def delete(self, name):
        self.deleted.append(name)


class FakeClient:
    """Cliente falso. `outcomes`: respuestas en orden (texto o excepción); si no, `text`/`error`."""

    def __init__(self, text=None, error=None, outcomes=None, files=None, listed=("m", "b"),
                 list_error=None):
        self.calls = []
        self.list_calls = 0
        self._listed, self._list_error = listed, list_error
        self._text, self._error = text, error
        self._outcomes = list(outcomes) if outcomes is not None else None
        self.models = self
        self.files = files or FakeFiles()

    def list(self):
        self.list_calls += 1
        if self._list_error:
            raise self._list_error
        return [SimpleNamespace(name=f"models/{n}", supported_actions=["generateContent"])
                for n in self._listed]

    def generate_content(self, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        if self._outcomes is not None:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return SimpleNamespace(text=outcome)
        if self._error:
            raise self._error
        return SimpleNamespace(text=self._text)


def _provider(text=None, error=None, outcomes=None, sleeps=None, files=None, download=None,
              video_dir=None, analyze_models=("m",), blind_models=("b",), listed=None,
              list_error=None, **kwargs):
    listed = listed if listed is not None else (*analyze_models, *blind_models)
    client = FakeClient(text=text, error=error, outcomes=outcomes, files=files, listed=listed,
                        list_error=list_error)
    sleep = sleeps.append if sleeps is not None else _no_sleep
    provider = GeminiProvider("clave-falsa", analyze_models=list(analyze_models),
                              blind_models=list(blind_models), client=client, sleep=sleep,
                              download=download or _no_download, video_dir=video_dir, **kwargs)
    return provider, client


ITEMS = [("pregunta", ["a", "b", "c", "d"])]


def _no_download(youtube_id, dest_dir):
    raise AssertionError("no debería bajar el video")


def _no_sleep(seconds):
    raise AssertionError(f"no debería esperar ({seconds} s)")


def test_analyze_video_parses_json():
    p, _ = _provider(json.dumps(_analysis()))
    result = p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert isinstance(result, VideoAnalysis)
    assert result.topics == ["Funciones"]
    assert result.questions[0].correct_index == 2


@pytest.mark.parametrize(
    "text",
    [
        "esto no es json",
        json.dumps(_analysis(questions=[_question(options=["1", "2", "3"])])),
        json.dumps(_analysis(questions=[_question(correct_index=4)])),
        json.dumps(_analysis(topics=[])),
        json.dumps(_analysis(topics=["a", "b", "c", "d"])),
        json.dumps(_analysis(questions=[_question(options=["1", "1", "2", "3"])])),
        json.dumps(_analysis(questions=[_question(options=["1", "", "2", "3"])])),
        json.dumps(_analysis(summary_md="  ")),
        json.dumps(_analysis(topics=["Funciones", " "])),
    ],
)
def test_invalid_response_raises_format_error(text):
    p, _ = _provider(text)
    with pytest.raises(AIFormatError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")


@pytest.mark.parametrize(
    "error",
    [
        errors.ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "x"}}),
        errors.ClientError(400, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "x"}}),
    ],
)
def test_429_raises_quota(error):
    p, _ = _provider(error=error)
    with pytest.raises(AIQuotaError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")


@pytest.mark.parametrize(
    "error, expected",
    [
        (errors.ServerError(500, {"error": {"message": "boom"}}), AIUnavailableError),
        (errors.ClientError(408, {"error": {"message": "x"}}), AIUnavailableError),
        (httpx.ReadTimeout("clave-falsa"), AIUnavailableError),
        (httpx.ConnectError("clave-falsa"), AIUnavailableError),
        (errors.ClientError(401, {"error": {"message": "x"}}), AIAuthError),
        (errors.ClientError(400, {"error": {"message": "x"}}), AIFormatError),
    ],
)
def test_error_classification_without_key(error, expected):
    p, _ = _provider(error=error)
    with pytest.raises(expected) as exc:
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert "clave-falsa" not in str(exc.value)
    assert exc.value.__cause__ is None


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(_analysis(questions=[])),
        json.dumps(_analysis(questions=[_question(stem="  ")])),
    ],
)
def test_empty_questions_or_stem_raise(text):
    p, _ = _provider(text)
    with pytest.raises(AIFormatError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")


def test_video_is_sent_as_youtube_uri():
    p, client = _provider(json.dumps(_analysis()))
    p.analyze_video("aaaaaaaaaa1", "Clase 1")
    call = client.calls[0]
    uris = [x.file_data.file_uri for x in call.contents.parts if x.file_data]
    assert uris == ["https://www.youtube.com/watch?v=aaaaaaaaaa1"]
    assert call.config.response_mime_type == "application/json"
    assert call.config.response_schema is VideoAnalysis
    assert call.config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW
    assert call.model == "m"


def _answers(*values):
    """Respuesta de la pasada a ciegas: un {"q", "answer"} por pregunta, en orden."""
    return json.dumps({"answers": [{"q": n, "answer": v} for n, v in enumerate(values, start=1)]})


def test_blind_batch_is_one_request_with_json_schema_and_temperature_zero():
    p, client = _provider(_answers(2, 0))
    items = [("pregunta 1", ["a", "b", "c", "d"]), ("pregunta 2", ["e", "f", "g", "h"])]
    assert p.answer_blind_batch(items) == [2, 0]
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.model == "b"
    assert call.config.temperature == 0
    assert call.config.response_mime_type == "application/json"
    assert call.config.response_schema is BlindAnswers
    assert "Pregunta 1: pregunta 1" in call.contents and "Pregunta 2: pregunta 2" in call.contents
    assert "0. e" in call.contents


def test_blind_batch_without_items_makes_no_request():
    p, client = _provider("nunca")
    assert p.answer_blind_batch([]) == []
    assert client.calls == []


@pytest.mark.parametrize(
    "text, expected",
    [
        (_answers(1, None, 2), [1, None, 2]),
        (_answers(1, 4, -1), [1, None, None]),  # fuera de rango: sin respuesta
        (_answers(True, "3", "x"), [None, 3, None]),
        (_answers(1.0, 2, 0), [None, 2, 0]),
        ('[{"q": 1, "answer": 0}, {"q": "2", "answer": 1}, {"q": 3, "answer": 2}]', [0, 1, 2]),
    ],
)
def test_blind_batch_maps_each_answer_and_none_when_unsure(text, expected):
    p, _ = _provider(text)
    items = [("p", ["a", "b", "c", "d"])] * 3
    assert p.answer_blind_batch(items) == expected


def _qs(*pairs):
    return json.dumps({"answers": [{"q": q, "answer": a} for q, a in pairs]})


@pytest.mark.parametrize("text", [
    "no sé",
    "",
    '{"answers": [1, 2, 3]}',                    # sin número de pregunta
    _qs((2, 1), (1, 2), (3, 3)),                 # desordenadas
    _qs((1, 1), (1, 2), (3, 3)),                 # pregunta repetida
    _qs((1, 1), (3, 3)),                         # falta una
    _qs((1, 1), (2, 2), (3, 3), (4, 0)),         # sobra una
    _qs((0, 1), (1, 2), (2, 3)),                 # numeradas desde 0
    _qs((True, 1), (2, 2), (3, 3)),              # q no es un número
    '{"otra": []}',
])
def test_unreadable_blind_batch_tries_the_next_model(text):
    p, client = _provider(outcomes=[text, _answers(1, 2, 3)], blind_models=("b", "c"))
    items = [("p", ["a", "b", "c", "d"])] * 3
    assert p.answer_blind_batch(items) == [1, 2, 3]
    assert [c.model for c in client.calls] == ["b", "c"]
    assert p.last_blind_model == "c"


def test_unreadable_blind_batch_on_every_model_is_format_error():
    p, client = _provider(outcomes=["no sé", "tampoco"], blind_models=("b", "c"))
    with pytest.raises(AIFormatError):
        p.answer_blind_batch(ITEMS)
    assert len(client.calls) == 2


def test_unreadable_plus_exhausted_is_quota_so_the_material_stays_pending():
    p, client = _provider(outcomes=["no sé", _daily_limit()], blind_models=("b", "c"),
                          clock=lambda: 1000.0)
    with pytest.raises(AIQuotaError) as exc:
        p.answer_blind_batch(ITEMS)
    assert exc.value.resets_at == 26000.0


def test_gemma_gets_plain_text_and_its_reply_is_read_tolerantly():
    reply = 'Claro, acá van:\n```json\n' + _answers(3, None) + '\n```\nSuerte.'
    p, client = _provider(reply, blind_models=("gemma-4-31b-it",))
    items = [("p1", ["a", "b", "c", "d"]), ("p2", ["a", "b", "c", "d"])]
    assert p.answer_blind_batch(items) == [3, None]
    config = client.calls[0].config
    assert config.response_schema is None and config.response_mime_type is None
    assert config.system_instruction is None
    assert config.temperature == 0


def test_model_that_rejects_json_mode_is_skipped():
    unsupported = errors.ClientError(400, {"error": {
        "code": 400, "status": "INVALID_ARGUMENT",
        "message": "JSON mode is not enabled for models/gemma-4-31b-it"}})
    p, client = _provider(outcomes=[unsupported, _answers(1)],
                          blind_models=("gemma-4-31b-it", "c"))
    assert p.answer_blind_batch(ITEMS) == [1]
    assert [c.model for c in client.calls] == ["gemma-4-31b-it", "c"]
    assert p.exhausted_until == {}


def test_blind_never_uses_the_model_that_made_the_analysis():
    p, client = _provider(_answers(1), blind_models=("m", "x", "b"))
    assert p.answer_blind_batch(ITEMS, exclude_models=("m", "x")) == [1]
    assert [c.model for c in client.calls] == ["b"]


def test_empty_blind_chain_after_exclusion_is_quota_not_a_failure():
    p, client = _provider(_answers(1), blind_models=("m",))
    with pytest.raises(AIQuotaError) as exc:
        p.answer_blind_batch(ITEMS, exclude_models=("m",))
    assert exc.value.resets_at is None
    assert client.calls == []


def test_exhaustion_mark_is_clamped_to_a_day_and_an_hour():
    p, _ = _provider(outcomes=[_daily_limit("999999s"), _answers(1)], blind_models=("b", "c"),
                     clock=lambda: 1000.0)
    p.answer_blind_batch(ITEMS)
    assert p.exhausted_until["b"] == 1000.0 + 86400 + 3600


def _bad_key_error(message="API key not valid. Please pass a valid API key.",
                   reason="API_KEY_INVALID"):
    return errors.ClientError(400, {"error": {
        "code": 400,
        "message": message,
        "status": "INVALID_ARGUMENT",
        "details": [{
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": reason,
            "domain": "googleapis.com",
            "metadata": {"service": "generativelanguage.googleapis.com"},
        }],
    }})


@pytest.mark.parametrize(
    "error",
    [
        _bad_key_error(),
        _bad_key_error("API key expired. Please renew the API key.", "API_KEY_EXPIRED"),
        _bad_key_error("Requests to this API are blocked.", "API_KEY_SERVICE_BLOCKED"),
        # Sin `details`, alcanza con el mensaje.
        errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                           "message": "API key expired. Please renew the API key."}}),
        errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                           "message": "API key not valid. Please pass a valid API key."}}),
    ],
)
def test_invalid_api_key_400_is_auth_error(error):
    p, _ = _provider(error=error)
    with pytest.raises(AIAuthError) as exc:
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert "clave-falsa" not in str(exc.value)
    assert "API key" not in str(exc.value)
    assert exc.value.__cause__ is None
    with pytest.raises(AIAuthError):
        p.answer_blind_batch(ITEMS)


def test_check_key_lists_models_without_generating():
    p, client = _provider("nunca")
    p.check_key()
    assert client.list_calls == 1
    assert client.calls == []


@pytest.mark.parametrize(
    "error, expected",
    [
        (_bad_key_error(), AIAuthError),
        (errors.ClientError(401, {"error": {"message": "x"}}), AIAuthError),
        (errors.ClientError(403, {"error": {"message": "x"}}), AIAuthError),
        (errors.ServerError(503, {"error": {"message": "x"}}), AIUnavailableError),
        (httpx.ConnectError("clave-falsa"), AIUnavailableError),
        (errors.ClientError(400, {"error": {"message": "x"}}), AIFormatError),
    ],
)
def test_check_key_maps_errors(error, expected):
    p, client = _provider(list_error=error)
    with pytest.raises(expected) as exc:
        p.check_key()
    assert "clave-falsa" not in str(exc.value)
    assert client.calls == []


def test_check_key_429_means_the_key_is_valid():
    p, client = _provider(list_error=_rate_limited("20s"))
    p.check_key()  # sin excepción y sin esperar
    assert client.calls == []


def test_invalid_questions_are_dropped_one_by_one():
    good = _question(stem="buena")
    bad = [
        _question(stem="tres opciones", options=["1", "2", "3"]),
        _question(stem="fuera de rango", correct_index=4),
        _question(stem="negativo", correct_index=-1),
        _question(stem="  "),
        _question(stem="repetidas", options=["1", "1", "2", "3"]),
        _question(stem="repetidas al normalizar", options=["x + 1", "X+1", "2", "3"]),
        _question(stem="vacía", options=["1", " ", "2", "3"]),
    ]
    p, _ = _provider(json.dumps(_analysis(questions=[bad[0], good, *bad[1:]])))
    result = p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert [q.stem for q in result.questions] == ["buena"]


def test_question_with_topic_outside_topics_is_kept():
    p, _ = _provider(json.dumps(_analysis(questions=[_question(topic="Otro tema")])))
    result = p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert [q.topic for q in result.questions] == ["Otro tema"]


def _rate_limited(delay="20s"):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []}]
    if delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    return errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded", "details": details,
    }})


def test_per_minute_429_waits_and_retries():
    sleeps = []
    p, client = _provider(outcomes=[_rate_limited("20s"), json.dumps(_analysis())], sleeps=sleeps)
    assert p.analyze_video("aaaaaaaaaa1", "Clase 1").topics == ["Funciones"]
    assert sleeps == [20.0]
    assert len(client.calls) == 2


def test_per_minute_429_gives_up_after_two_retries():
    sleeps = []
    p, client = _provider(outcomes=[_rate_limited("7.5s")] * 3, sleeps=sleeps)
    with pytest.raises(AIQuotaError) as exc:
        p.answer_blind_batch(ITEMS)
    assert sleeps == [7.5, 7.5]
    assert len(client.calls) == 3
    assert exc.value.resets_at is None
    assert p.exhausted_until == {}  # el límite por minuto no marca el modelo


@pytest.mark.parametrize("delay", [None, "61s", "3600s", "basura"])
def test_unknown_429_is_quota_without_waiting(delay):
    p, client = _provider(outcomes=[_rate_limited(delay)])
    with pytest.raises(AIQuotaError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert len(client.calls) == 1


def _daily_limit(delay="25000s"):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        "quotaValue": "20"}]}]
    if delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    return errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded", "details": details,
    }})


def test_daily_429_marks_the_model_and_moves_to_the_next_one():
    p, client = _provider(outcomes=[_daily_limit("25000s"), json.dumps(_analysis())],
                          analyze_models=("m", "n"), clock=lambda: 1000.0)
    assert p.analyze_video("aaaaaaaaaa1", "Clase 1").topics == ["Funciones"]
    assert [c.model for c in client.calls] == ["m", "n"]
    assert p.exhausted_until == {"m": 26000.0}
    assert p.last_analysis_model == "n"


def test_an_exhausted_model_is_not_called_until_it_resets():
    now = {"t": 1000.0}
    p, client = _provider(text=json.dumps(_analysis()), analyze_models=("m", "n"),
                          clock=lambda: now["t"])
    p.exhausted_until["m"] = 2000.0
    p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert [c.model for c in client.calls] == ["n"]
    now["t"] = 2000.5
    p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert [c.model for c in client.calls] == ["n", "m"]


def test_daily_429_without_retry_delay_waits_until_pacific_midnight():
    p, _ = _provider(outcomes=[_daily_limit(None), _answers(1)],
                     blind_models=("b", "c"), clock=lambda: 1_800_000_000.0)
    p.answer_blind_batch(ITEMS)
    wait = p.exhausted_until["b"] - 1_800_000_000.0
    assert 60 <= wait <= 86400


def test_quota_error_only_when_the_whole_chain_is_exhausted():
    p, client = _provider(outcomes=[_daily_limit("500s"), _daily_limit("300s")],
                          analyze_models=("m", "n"), clock=lambda: 1000.0)
    with pytest.raises(AIQuotaError) as exc:
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert exc.value.resets_at == 1300.0  # el primero que vuelve
    assert len(client.calls) == 2
    # Ya marcados: el próximo pedido no gasta nada.
    with pytest.raises(AIQuotaError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert len(client.calls) == 2


def test_only_listed_models_are_used_and_the_listing_is_cached():
    p, client = _provider(_answers(1), blind_models=("no-existe", "b"), listed=("m", "b"))
    assert p.answer_blind_batch(ITEMS) == [1]
    assert [c.model for c in client.calls] == ["b"]
    other = GeminiProvider("clave-falsa", analyze_models=["m"], blind_models=["b"], client=client,
                           sleep=_no_sleep)
    other.answer_blind_batch(ITEMS)
    assert client.list_calls == 1  # una vez por proceso y por clave


def test_failed_listing_falls_back_to_the_configured_chain():
    p, client = _provider(_answers(1), list_error=httpx.ConnectError("x"))
    assert p.answer_blind_batch(ITEMS) == [1]
    assert [c.model for c in client.calls] == ["b"]


def test_analyze_400_is_video_unavailable():
    p, _ = _provider(error=errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                              "message": "Request contains an invalid argument."}}))
    with pytest.raises(AIVideoUnavailableError) as exc:
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert exc.value.__cause__ is None


def test_blind_400_is_plain_format_error():
    p, _ = _provider(error=errors.ClientError(400, {"error": {"message": "x"}}))
    with pytest.raises(AIFormatError) as exc:
        p.answer_blind_batch(ITEMS)
    assert not isinstance(exc.value, AIVideoUnavailableError)


# --- Ruta local: Gemini no acepta el enlace de YouTube con esta clave -------------

def _denied():
    return errors.ClientError(403, {"error": {
        "code": 403, "status": "PERMISSION_DENIED", "message": "The caller does not have permission",
    }})


def _overloaded():
    return errors.ServerError(503, {"error": {
        "code": 503, "status": "UNAVAILABLE",
        "message": "The model is overloaded due to high demand. Please try again later.",
    }})


class FakeDownloader:
    """Baja "videos" de mentira a `dest_dir`, o falla si se le pide."""

    def __init__(self, error=None):
        self.error, self.calls = error, []

    def __call__(self, youtube_id, dest_dir):
        self.calls.append((youtube_id, dest_dir))
        if self.error:
            raise self.error
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{youtube_id}.mp4"
        path.write_bytes(b"video")
        return path


def _file_uris(call):
    return [x.file_data.file_uri for x in call.contents.parts if x.file_data]


def test_403_on_youtube_url_falls_back_to_upload(tmp_path):
    files, download, sleeps = FakeFiles(polls=2), FakeDownloader(), []
    p, client = _provider(outcomes=[_denied(), json.dumps(_analysis())], files=files,
                          download=download, video_dir=tmp_path, sleeps=sleeps)
    result = p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert result.topics == ["Funciones"]
    assert _file_uris(client.calls[0]) == ["https://www.youtube.com/watch?v=aaaaaaaaaa1"]
    assert _file_uris(client.calls[1]) == ["https://gemini/files/abc"]
    assert client.calls[1].config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW
    assert download.calls == [("aaaaaaaaaa1", tmp_path)]
    assert files.uploaded == [(tmp_path / "aaaaaaaaaa1.mp4", "video/mp4")]
    assert files.deleted == ["files/abc"]
    assert sleeps and all(s > 0 for s in sleeps)  # esperó a que quede ACTIVE
    assert not (tmp_path / "aaaaaaaaaa1.mp4").exists()  # se borra al terminar bien
    assert p.youtube_url_denied is True


def test_after_a_denial_the_next_video_skips_the_url(tmp_path):
    files = FakeFiles(polls=0)
    p, client = _provider(outcomes=[_denied(), json.dumps(_analysis()), json.dumps(_analysis())],
                          files=files, download=FakeDownloader(), video_dir=tmp_path)
    p.analyze_video("aaaaaaaaaaa", "Clase 1")
    p.analyze_video("bbbbbbbbbbb", "Clase 2")
    assert len(client.calls) == 3
    assert _file_uris(client.calls[2]) == ["https://gemini/files/abc"]


def test_remembered_denial_goes_straight_to_upload(tmp_path):
    p, client = _provider(outcomes=[json.dumps(_analysis())], files=FakeFiles(polls=0),
                          download=FakeDownloader(), video_dir=tmp_path, youtube_url_denied=True)
    p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert [_file_uris(c) for c in client.calls] == [["https://gemini/files/abc"]]


def test_400_about_the_youtube_url_falls_back_without_remembering(tmp_path):
    error = errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                               "message": "Unsupported YouTube URL for this request."}})
    p, client = _provider(outcomes=[error, json.dumps(_analysis())], files=FakeFiles(polls=0),
                          download=FakeDownloader(), video_dir=tmp_path)
    assert p.analyze_video("aaaaaaaaaa1", "Clase 1").topics == ["Funciones"]
    assert p.youtube_url_denied is False


def test_remote_file_is_deleted_even_when_the_analysis_fails(tmp_path):
    files = FakeFiles(polls=0)
    p, _ = _provider(outcomes=[_denied(), "esto no es json"], files=files,
                     download=FakeDownloader(), video_dir=tmp_path)
    with pytest.raises(AIFormatError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert files.deleted == ["files/abc"]


def test_remote_file_is_deleted_when_it_never_becomes_active(tmp_path):
    files = FakeFiles(polls=1, final_state=types.FileState.FAILED)
    p, client = _provider(outcomes=[_denied()], files=files, download=FakeDownloader(),
                          video_dir=tmp_path, sleeps=[])
    with pytest.raises(AIVideoUnavailableError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert files.deleted == ["files/abc"]
    assert len(client.calls) == 1


def test_processing_timeout_is_video_unavailable_and_deletes(tmp_path):
    sleeps = []
    files = FakeFiles(polls=10_000)
    p, _ = _provider(outcomes=[_denied()], files=files, download=FakeDownloader(),
                     video_dir=tmp_path, sleeps=sleeps)
    with pytest.raises(AIVideoUnavailableError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert 290 <= sum(sleeps) <= 310
    assert files.deleted == ["files/abc"]


def test_transient_error_after_upload_keeps_the_local_video_for_next_time(tmp_path):
    files = FakeFiles(polls=0)
    p, _ = _provider(outcomes=[_denied(), errors.ServerError(500, {"error": {"message": "x"}})],
                     files=files, download=FakeDownloader(), video_dir=tmp_path)
    with pytest.raises(AIUnavailableError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert files.deleted == ["files/abc"]
    assert (tmp_path / "aaaaaaaaaa1.mp4").exists()


def test_download_failure_is_video_unavailable(tmp_path):
    files = FakeFiles()
    p, _ = _provider(outcomes=[_denied()], files=files,
                     download=FakeDownloader(VideoDownloadError("privado")), video_dir=tmp_path)
    with pytest.raises(AIVideoUnavailableError) as exc:
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert exc.value.__cause__ is None
    assert files.uploaded == []


@pytest.mark.parametrize("error", [
    errors.ServerError(500, {"error": {"message": "x"}}),
    httpx.ConnectError("clave-falsa"),
])
def test_upload_failure_is_video_unavailable(tmp_path, error):
    files = FakeFiles(upload_error=error)
    p, _ = _provider(outcomes=[_denied()], files=files,
                     download=FakeDownloader(), video_dir=tmp_path)
    with pytest.raises(AIVideoUnavailableError) as exc:
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert "clave-falsa" not in str(exc.value)
    assert not (tmp_path / "aaaaaaaaaa1.mp4").exists()  # el material falla: no se guarda
    assert files.deleted == []  # no llegó a subirse


def test_unexpected_error_after_upload_removes_local_and_remote(tmp_path):
    class BrokenFiles(FakeFiles):
        def get(self, name):
            raise RuntimeError("inesperado")

    files = BrokenFiles(polls=1)
    p, _ = _provider(outcomes=[_denied()], files=files, download=FakeDownloader(),
                     video_dir=tmp_path, sleeps=[])
    with pytest.raises(RuntimeError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert files.deleted == ["files/abc"]
    assert not (tmp_path / "aaaaaaaaaa1.mp4").exists()


def test_403_on_video_is_never_an_auth_error(tmp_path):
    p, _ = _provider(outcomes=[_denied(), _denied()], files=FakeFiles(polls=0),
                     download=FakeDownloader(), video_dir=tmp_path)
    with pytest.raises(AIVideoUnavailableError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")


def test_check_key_403_is_still_an_auth_error():
    p, _ = _provider(list_error=_denied())
    with pytest.raises(AIAuthError):
        p.check_key()


def test_503_high_demand_waits_and_retries():
    sleeps = []
    p, client = _provider(outcomes=[_overloaded(), json.dumps(_analysis())], sleeps=sleeps)
    assert p.analyze_video("aaaaaaaaaa1", "Clase 1").topics == ["Funciones"]
    assert sleeps == [20.0]
    assert len(client.calls) == 2


def test_503_gives_up_after_one_retry():
    sleeps = []
    p, client = _provider(outcomes=[_overloaded()] * 2, sleeps=sleeps)
    with pytest.raises(AIUnavailableError):
        p.answer_blind_batch(ITEMS)
    assert sleeps == [20.0]
    assert len(client.calls) == 2
    assert p.exhausted_until == {}


def _deadline_exceeded():
    return errors.ServerError(504, {"error": {
        "code": 504, "status": "DEADLINE_EXCEEDED", "message": "Deadline expired before operation could complete."}})


def test_504_retries_once_then_moves_to_the_next_model():
    sleeps = []
    p, client = _provider(outcomes=[_deadline_exceeded(), _deadline_exceeded(), json.dumps(_analysis())],
                          analyze_models=("m", "n"), sleeps=sleeps)
    assert p.analyze_video("aaaaaaaaaa1", "Clase 1").topics == ["Funciones"]
    assert [c.model for c in client.calls] == ["m", "m", "n"]
    assert sleeps == [20.0]
    assert p.exhausted_until == {}  # saturado no es agotado
    assert p.last_analysis_model == "n"


def test_overloaded_and_exhausted_chain_is_unavailable_not_quota():
    p, _ = _provider(outcomes=[_daily_limit(), _overloaded(), _overloaded()],
                     analyze_models=("m", "n"), sleeps=[])
    with pytest.raises(AIUnavailableError):
        p.analyze_video("aaaaaaaaaa1", "Clase 1")


def test_model_without_video_is_skipped_and_the_upload_is_reused(tmp_path):
    files = FakeFiles(polls=0)
    unsupported = errors.ClientError(400, {"error": {
        "code": 400, "status": "INVALID_ARGUMENT", "message": "Video input is not supported by this model."}})
    p, client = _provider(outcomes=[unsupported, json.dumps(_analysis())], analyze_models=("m", "n"),
                          files=files, download=FakeDownloader(), video_dir=tmp_path,
                          youtube_url_denied=True)
    assert p.analyze_video("aaaaaaaaaa1", "Clase 1").topics == ["Funciones"]
    assert [c.model for c in client.calls] == ["m", "n"]
    assert [_file_uris(c) for c in client.calls] == [["https://gemini/files/abc"]] * 2
    assert len(files.uploaded) == 1
    assert files.deleted == ["files/abc"]
    assert p.exhausted_until == {}


def test_daily_429_after_upload_reuses_the_file_for_the_next_model(tmp_path):
    files = FakeFiles(polls=0)
    p, client = _provider(outcomes=[_daily_limit(), json.dumps(_analysis())], analyze_models=("m", "n"),
                          files=files, download=FakeDownloader(), video_dir=tmp_path,
                          youtube_url_denied=True)
    p.analyze_video("aaaaaaaaaa1", "Clase 1")
    assert len(files.uploaded) == 1
    assert files.deleted == ["files/abc"]
