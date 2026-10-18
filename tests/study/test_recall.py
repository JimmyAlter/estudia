from estudia.study.recall import RecallHeading, normalize_words, recall_check, same_word, summary_headings

SUMMARY = "# Funciones lineales\ntexto\n## **Raíces** y ceros\n### Pendiente\n"


def test_normalize_strips_accents_case_and_stopwords():
    assert normalize_words("La Función Lineal y sus raíces") == {"funcion", "lineal", "raices"}


def test_normalize_handles_student_math_notation():
    assert normalize_words("Vale 2√2 y x² con π") == {"vale", "2", "x2"}
    assert normalize_words("√ π ²") == {"2"}


def test_headings_from_markdown_levels():
    md = "# Uno\n## **Dos** cosas ##\n###### Seis\ntexto # no\n```\n# código\n```\n####### siete\n"
    assert summary_headings(md) == ["Uno", "Dos cosas", "Seis"]


def test_same_word():
    assert same_word("funcion", "funciones")
    assert not same_word("sol", "solucion")
    assert not same_word("raiz", "raices")


def test_heading_mentioned_by_plural():
    r = recall_check("# Funciones lineales", "la funcion lineal corta")
    assert r == [RecallHeading("Funciones lineales", True)]


def test_heading_not_mentioned_is_marked():
    r = recall_check(SUMMARY, "hablamos de la funcion lineal")
    assert [(h.title, h.mentioned) for h in r] == [
        ("Funciones lineales", True), ("Raíces y ceros", False), ("Pendiente", False)]


def test_half_of_the_words_is_enough():
    assert recall_check("# Raíces cuadradas y cúbicas", "raices cubicas")[0].mentioned
    assert not recall_check("# Raíces cuadradas y cúbicas", "raices")[0].mentioned


def test_title_only_stopwords_never_marked():
    assert recall_check("# Y el de la", "nada")[0].mentioned
    assert recall_check("# π", "nada")[0].mentioned


def test_recall_headings_ignore_page_marks():
    md = "## Límites (pág. 14)\n## Continuidad (pág. 3) ##\n## Teorema (pág. 2) de Bolzano\n"
    assert summary_headings(md) == ["Límites", "Continuidad", "Teorema (pág. 2) de Bolzano"]
    # En diapositivas la marca es "(diap. N)".
    assert summary_headings("## Límites (diap. 4)\n") == ["Límites"]
