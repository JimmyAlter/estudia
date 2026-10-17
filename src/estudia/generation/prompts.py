"""Textos de los prompts que se le mandan al modelo."""

ANALYSIS_PROMPT = """\
Sos un docente universitario que prepara material de estudio a partir de la \
clase en video "{title}". Mirá y escuchá el video completo y devolvé un JSON \
con el resumen, los temas y las preguntas.

Reglas:
- Las fórmulas se leen de las diapositivas que se ven en el video, no del \
audio: el audio suele ser ambiguo con los símbolos. Si una fórmula de la \
diapositiva contradice lo que se dice, vale la diapositiva.
- Notación: todo lo que lee el estudiante ("summary_md", "stem", "options", \
"explanation", "common_error") va con la notación matemática de clase, nunca \
con notación de programación. Exponentes con superíndices (x², x³, xⁿ); \
producto con "·" o implícito (2x, a·b), nunca "*"; "≠", "→", "≤", "≥", "√" y \
el signo menos "−". Sin backticks, sin LaTeX y sin código. Por ejemplo: \
f(x) = 3x² − 2·a + √2, y no f(x) = 3*x**2 - 2*a + sqrt(2).
- "summary_md": resumen de la clase en Markdown simple (títulos, viñetas, \
negrita), en castellano rioplatense.
- "topics": entre 1 y 3 temas de la clase, con nombres cortos y mayúscula \
solo al principio (como una oración: "Gráfica de la función cuadrática").
- "questions": entre 8 y 12 preguntas de opción múltiple sobre lo que enseña \
la clase, no sobre detalles del video (voz, orden de las diapositivas).
- Cada pregunta tiene exactamente 4 opciones distintas y una sola correcta; \
"correct_index" va de 0 a 3.
- Los distractores deben corresponder a errores típicos de los estudiantes, \
no a opciones absurdas. En "common_error" explicá cuál es el error típico.
- "explanation": explicá por qué la opción correcta es correcta.
- "topic": debe ser exactamente uno de los textos de "topics".
- "source_seconds": segundo del video en el que la clase explica el tema de \
la pregunta.
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

BLIND_BATCH_PROMPT = """\
Resolvé estas {n} preguntas de opción múltiple. Cada una tiene una sola opción \
correcta. Devolvé solo un JSON con esta forma, sin explicación:
{{"answers": [{{"q": 1, "answer": <opción>}}, {{"q": 2, "answer": <opción>}}, ...]}}
Va un elemento por pregunta, en orden, con "q" del 1 al {n} (el número de la \
pregunta). "answer" es el número de la opción correcta (0, 1, 2 o 3), o null si \
no estás seguro.

{questions}
"""
