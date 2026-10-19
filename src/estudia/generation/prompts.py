"""Textos de los prompts que se le mandan al modelo."""

# Reglas que comparten el prompt de video y el de documentos. ANALYSIS_PROMPT se arma con
# ellas y queda idéntico byte por byte (lo cuida test_video_prompt_unchanged).
_NOTATION_RULE = """\
- Notación: todo lo que lee el estudiante ("summary_md", "stem", "options", \
"explanation", "common_error") va con la notación matemática de clase, nunca \
con notación de programación. Exponentes con superíndices (x², x³, xⁿ); \
producto con "·" o implícito (2x, a·b), nunca "*"; "≠", "→", "≤", "≥", "√" y \
el signo menos "−". Sin backticks, sin LaTeX y sin código. Por ejemplo: \
f(x) = 3x² − 2·a + √2, y no f(x) = 3*x**2 - 2*a + sqrt(2).
"""

_TOPICS_RULE = """\
- "topics": entre 1 y 3 temas de la clase, con nombres cortos y mayúscula \
solo al principio (como una oración: "Gráfica de la función cuadrática").
"""

_QUESTION_RULES = """\
- Cada pregunta tiene exactamente 4 opciones distintas y una sola correcta; \
"correct_index" va de 0 a 3.
- Los distractores deben corresponder a errores típicos de los estudiantes, \
no a opciones absurdas. En "common_error" explicá cuál es el error típico.
- "explanation": explicá por qué la opción correcta es correcta.
- "topic": debe ser exactamente uno de los textos de "topics".
"""

_CHECK_EXPR_RULE = """\
- "check_expr": completalo SOLO si las 4 opciones son valores numéricos o \
algebraicos cerrados escritos solos (un número o una expresión cerrada como \
2*sqrt(2), sin variables, sin unidades, sin %, sin palabras, sin pares ni \
tuplas, sin conjuntos ni intervalos). Escribilo \
en sintaxis SymPy usando solamente + - * / ** sqrt pi Rational Integer y \
literales enteros (exponentes enteros chicos). Tiene que valer exactamente lo \
mismo que la opción correcta. En cualquier otro caso, poné null. "check_expr" \
es el único campo con sintaxis SymPy: si la opción correcta es "2√2", \
"check_expr" es "2*sqrt(2)"; si es "3²", es "3**2".
"""

# Variantes para documentos: donde la regla compartida dice "clase", el apunte dice "apunte".
_NOTATION_RULE_DOC = _NOTATION_RULE.replace("notación matemática de clase", "notación matemática del apunte")
_TOPICS_RULE_DOC = _TOPICS_RULE.replace("temas de la clase", "temas del material")

ANALYSIS_PROMPT = """\
Sos un docente universitario que prepara material de estudio a partir de la \
clase en video "{title}". Mirá y escuchá el video completo y devolvé un JSON \
con el resumen, los temas y las preguntas.

Reglas:
- Las fórmulas se leen de las diapositivas que se ven en el video, no del \
audio: el audio suele ser ambiguo con los símbolos. Si una fórmula de la \
diapositiva contradice lo que se dice, vale la diapositiva.
""" + _NOTATION_RULE + """\
- "summary_md": resumen de la clase en Markdown simple (títulos, viñetas, \
negrita), en castellano rioplatense.
""" + _TOPICS_RULE + """\
- "questions": entre 8 y 12 preguntas de opción múltiple sobre lo que enseña \
la clase, no sobre detalles del video (voz, orden de las diapositivas).
""" + _QUESTION_RULES + """\
- "source_seconds": segundo del video en el que la clase explica el tema de \
la pregunta.
- "segments": los tramos de la clase en orden, cada uno con "start_s" y \
"end_s" (segundos del video) y un "title" corto; sin superponerse.
""" + _CHECK_EXPR_RULE

# `{source}`: "el apunte en PDF" o "las diapositivas"; `{pages_note}`: en un tramo,
# " Este archivo tiene N páginas." (vacío en un documento entero).
DOCUMENT_PROMPT = """\
Sos un docente universitario que prepara material de estudio a partir de un \
material de clase: {source} "{title}". Leé el material completo y devolvé un \
JSON con el resumen, los temas y las preguntas.{pages_note}

Reglas:
""" + _NOTATION_RULE_DOC + """\
- "summary_md": resumen del material en Markdown simple (títulos, viñetas, \
negrita), en castellano rioplatense.
- En "summary_md", cada título termina con la página donde empieza, así: (pág. 14).
""" + _TOPICS_RULE_DOC + """\
- "questions": entre 8 y 12 preguntas de opción múltiple sobre lo que enseña \
el material, no sobre detalles del archivo (formato, orden de las páginas).
""" + _QUESTION_RULES + """\
- "source_page": número de página del archivo (la primera es 1) donde se explica el tema de la pregunta; en diapositivas, el número de diapositiva.
- Contá las páginas desde la primera hoja de este archivo (la primera es la 1); \
ignorá los números impresos en las hojas.
""" + _CHECK_EXPR_RULE

BLIND_BATCH_PROMPT = """\
Resolvé estas {n} preguntas de opción múltiple. Cada una tiene una sola opción \
correcta. Devolvé solo un JSON con esta forma, sin explicación:
{{"answers": [{{"q": 1, "answer": <opción>}}, {{"q": 2, "answer": <opción>}}, ...]}}
Va un elemento por pregunta, en orden, con "q" del 1 al {n} (el número de la \
pregunta). "answer" es el número de la opción correcta (0, 1, 2 o 3), o null si \
no estás seguro.

{questions}
"""

# `{course_topics}`: los temas de la materia, uno por renglón (o un aviso si no hay).
STYLE_PROMPT = """\
Sos un docente universitario. Te paso un parcial viejo de la materia (un PDF o \
una foto). No te pido que lo resuelvas: solo quiero saber cómo es el formato \
del examen. Devolvé un JSON con esta información:

- "exercises": un elemento por cada ejercicio del parcial, en orden. Contá los \
ejercicios y clasificá cada uno en "kind": "opcion_multiple" (elegir entre \
opciones), "verdadero_falso" (decidir si una afirmación es verdadera o falsa) \
o "desarrollo" (hay que resolver, justificar o demostrar). En "points", el \
puntaje del ejercicio si figura en el parcial; si no figura, null.
- "minutes": el tiempo para resolverlo, en minutos, si figura; si no, null.
- "max_grade": la nota máxima del parcial (por ejemplo 10 o 100); si no \
figura, null.
- "topics": los temas que evalúa. Si un tema coincide con uno de los temas de \
la materia de esta lista, usá exactamente ese nombre; si no coincide con \
ninguno, escribilo con un nombre corto.
{course_topics}
- "formula_sheet": true si el parcial menciona que se puede usar una hoja de \
fórmulas (o un resumen o apunte permitido); si no, false.

Reglas:
- No transcribas ningún enunciado: ni consignas, ni datos, ni fórmulas, ni \
respuestas, ni nombres de personas. Solo cantidades, tipos, puntajes y nombres \
cortos de temas.
- Si el archivo no es un examen o no se puede leer, devolvé "exercises" vacío.
"""

# Preguntas con el estilo del examen. `{kinds}`: cuántas de cada tipo, una por renglón;
# `{topics}`: los temas, uno por renglón; `{formula_sheet}`: una línea sobre la hoja de
# fórmulas; `{material}`: los resúmenes de la clase de esos temas (lo único en que se apoya).
STYLE_QUESTIONS_PROMPT = """\
Sos un docente universitario de "{course}" que prepara preguntas de práctica \
con el formato del examen de la cátedra. Devolvé un JSON con "questions".

Cuántas preguntas y de qué tipo ("kind"):
{kinds}

Temas ("topics"):
{topics}

{formula_sheet}

Reglas:
- Usá solo el material de la clase que va abajo; no inventes contenido que no esté.
- Escribí preguntas nuevas: no copies enunciados de parciales ni de exámenes viejos.
- Todas las preguntas tienen 4 opciones y una sola correcta, también las de \
verdadero/falso y las de desarrollo:
  - "opcion_multiple": una pregunta de opción múltiple sobre lo que enseña el material.
  - "verdadero_falso": el enunciado es "¿Cuál de estas afirmaciones es verdadera?" \
y las 4 opciones son afirmaciones: una sola es verdadera según el material.
  - "desarrollo": un problema de varios pasos con un resultado final único. La \
opción correcta es ese resultado y las otras 3 son resultados de errores típicos \
en algún paso.
- "kind": el tipo de la pregunta, exactamente uno de los de arriba.
""" + _NOTATION_RULE + _QUESTION_RULES + _CHECK_EXPR_RULE + """\
- En "desarrollo", completá "check_expr" cuando el resultado es numérico o \
algebraico cerrado (con las reglas de arriba).

Material de la clase:
{material}
"""
