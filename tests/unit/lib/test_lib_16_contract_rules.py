"""LIB-16: typed query contract by rules. Invented names only; English, German, Dutch, Spanish."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memspine.read.contract_rules import (
    build_contract,
    render_header,
)

NOW = datetime(2025, 5, 14, 10, 0, tzinfo=UTC)  # a Wednesday


def D(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


# (query, language, answer_type, shape, view kind, view start, view end, personal, request kind)
U = "unspecified"
# fmt: off
CASES: list[tuple[str, str, str, str, str, datetime | None, datetime | None, bool | None, str]] = [
    # ---- English
    ("When did Ilse start her job at Quorvex?", "en", "date", "single", U, None, None, True, "lookup"),
    ("How long did Bram live in Zorbia?", "en", "duration", "single", U, None, None, True, "lookup"),
    ("How many times did I visit Lumea in 2023?", "en", "count", "list_count", "range", D(2023, 1, 1), D(2024, 1, 1), True, "enumeration"),
    ("List all the pets that Ilse has owned", "en", "list", "list_count", U, None, None, True, "enumeration"),
    ("Do I still work at Quorvex?", "en", "yesno", "single", U, None, None, True, "lookup"),
    ("Where do I live now?", "en", "entity", "latest", "current", None, None, True, "lookup"),
    ("What was my address as of 2021-03-04?", "en", "entity", "as_of", "as_of", None, None, True, "as_of"),
    ("Show the history of my job", "en", "unknown", "history", "history", None, None, True, "enumeration"),
    ("Compare Ilse's salary with Bram's salary", "en", "unknown", "comparison", U, None, None, True, "lookup"),
    ("Who is older than Bram, Ilse or Marta?", "en", "entity", "comparison", U, None, None, True, "lookup"),
    ("What do Ilse and Bram have in common?", "en", "entity", "join", U, None, None, True, "lookup"),
    ("How long between my move to Lumea and my new job?", "en", "duration", "join", U, None, None, True, "lookup"),
    ("What is the capital of Freedonia?", "en", "entity", "single", U, None, None, False, "lookup"),
    ("Who wrote the novel Zarnak Rising?", "en", "entity", "single", U, None, None, False, "lookup"),
    ("Tell me everything you know about Ilse", "en", "list", "list_count", U, None, None, True, "show_all"),
    ("What did I eat yesterday?", "en", "entity", "single", "range", D(2025, 5, 13), D(2025, 5, 14), True, "as_of"),
    ("Why did Bram leave Quorvex?", "en", "free", "single", U, None, None, True, "lookup"),
    ("When did I last visit Zorbia?", "en", "date", "latest", U, None, None, True, "lookup"),
    ("Which restaurants did we try in March 2024?", "en", "list", "list_count", "range", D(2024, 3, 1), D(2024, 4, 1), True, "enumeration"),
    ("How old is Bram?", "en", "unknown", "unknown", U, None, None, True, "lookup"),
    ("Is Ilse vegetarian or vegan?", "en", "entity", "single", U, None, None, True, "lookup"),
    ("What happened before 2020?", "en", "entity", "single", "range", None, D(2020, 1, 1), None, "as_of"),
    ("Where was I living back in 2019?", "en", "entity", "as_of", "as_of", None, None, True, "as_of"),
    ("Blorp?", "unknown", "unknown", "unknown", U, None, None, None, "lookup"),
    ("What changed in Ilse's life since 2022?", "en", "entity", "single", "range", D(2022, 1, 1), None, True, "as_of"),
    ("Between 2018 and 2020, how many jobs did I have?", "en", "count", "list_count", "range", D(2018, 1, 1), D(2021, 1, 1), True, "enumeration"),
    ("Can you explain how a heat pump works?", "en", "unknown", "unknown", U, None, None, False, "lookup"),
    ("What are my current medications?", "en", "entity", "latest", "current", None, None, True, "lookup"),
    ("What am I doing next Friday?", "en", "entity", "single", "range", D(2025, 5, 16), D(2025, 5, 17), True, "as_of"),
    ("How did my address change over time?", "en", "free", "history", "history", None, None, True, "enumeration"),
    # ---- German
    ("Wann hat Ilse bei Quorvex angefangen?", "de", "date", "single", U, None, None, True, "lookup"),
    ("Wie lange wohnt Bram schon in Zorbia?", "de", "duration", "single", U, None, None, True, "lookup"),
    ("Wie viele Bücher habe ich 2023 gelesen?", "de", "count", "list_count", "range", D(2023, 1, 1), D(2024, 1, 1), True, "enumeration"),
    ("Liste alle Haustiere von Ilse auf", "de", "list", "list_count", U, None, None, True, "enumeration"),
    ("Hat Bram einen Hund?", "de", "yesno", "single", U, None, None, True, "lookup"),
    ("Wo wohne ich jetzt?", "de", "entity", "latest", "current", None, None, True, "lookup"),
    ("Wie war meine Adresse zum Stichtag 04.03.2021?", "de", "free", "as_of", "as_of", None, None, True, "as_of"),
    ("Zeig mir den Verlauf meines Jobs", "de", "unknown", "history", "history", None, None, True, "enumeration"),
    ("Wer ist älter als Bram, Ilse oder Marta?", "de", "entity", "comparison", U, None, None, True, "lookup"),
    ("Was haben Ilse und Bram gemeinsam?", "de", "entity", "join", U, None, None, True, "lookup"),
    ("Wie lange nach meinem Umzug nach Zorbia habe ich den Job gewechselt?", "de", "duration", "join", U, None, None, True, "lookup"),
    ("Was ist die Hauptstadt von Freedonia?", "de", "entity", "single", U, None, None, False, "lookup"),
    ("Erzähl mir alles über Ilse", "de", "list", "list_count", U, None, None, True, "show_all"),
    ("Was habe ich gestern gegessen?", "de", "entity", "single", "range", D(2025, 5, 13), D(2025, 5, 14), True, "as_of"),
    ("Warum hat Bram Quorvex verlassen?", "de", "free", "single", U, None, None, True, "lookup"),
    ("Wann war ich zuletzt in Zorbia?", "de", "date", "latest", U, None, None, True, "lookup"),
    ("Welche Restaurants haben wir im März 2024 probiert?", "de", "list", "list_count", "range", D(2024, 3, 1), D(2024, 4, 1), True, "enumeration"),
    ("Wo ist Zorbia?", "de", "entity", "single", U, None, None, None, "lookup"),
    ("Ist Ilse Vegetarierin oder Veganerin?", "de", "entity", "single", U, None, None, True, "lookup"),
    ("Erkläre, wie eine Wärmepumpe funktioniert", "de", "unknown", "unknown", U, None, None, False, "lookup"),
    ("Was hat sich seit 2022 an Ilses Gehalt geändert?", "de", "entity", "history", "history", D(2022, 1, 1), None, True, "enumeration"),
    ("Wie viel kostet Ilses Miete?", "de", "free", "single", U, None, None, True, "lookup"),
    # ---- Dutch
    ("Wanneer is Ilse bij Quorvex begonnen?", "nl", "date", "single", U, None, None, True, "lookup"),
    ("Hoe lang woont Bram al in Zorbia?", "nl", "duration", "single", U, None, None, True, "lookup"),
    ("Hoeveel boeken heb ik in 2023 gelezen?", "nl", "count", "list_count", "range", D(2023, 1, 1), D(2024, 1, 1), True, "enumeration"),
    ("Noem alle huisdieren van Ilse", "nl", "list", "list_count", U, None, None, True, "enumeration"),
    ("Heeft Bram een hond?", "nl", "yesno", "single", U, None, None, True, "lookup"),
    ("Waar woon ik nu?", "nl", "entity", "latest", "current", None, None, True, "lookup"),
    ("Wat was mijn adres per 4 maart 2021?", "nl", "entity", "as_of", "as_of", None, None, True, "as_of"),
    ("Toon de geschiedenis van mijn baan", "nl", "unknown", "history", "history", None, None, True, "enumeration"),
    ("Wie is ouder dan Bram, Ilse of Marta?", "nl", "entity", "comparison", U, None, None, True, "lookup"),
    ("Wat hebben Ilse en Bram gemeen?", "nl", "entity", "join", U, None, None, True, "lookup"),
    ("Hoe lang na mijn verhuizing naar Zorbia ben ik van baan veranderd?", "nl", "duration", "join", U, None, None, True, "lookup"),
    ("Wat is de hoofdstad van Freedonia?", "nl", "entity", "single", U, None, None, False, "lookup"),
    ("Vertel me alles over Ilse", "nl", "list", "list_count", U, None, None, True, "show_all"),
    ("Wat heb ik gisteren gegeten?", "nl", "entity", "single", "range", D(2025, 5, 13), D(2025, 5, 14), True, "as_of"),
    ("Waarom heeft Bram Quorvex verlaten?", "nl", "free", "single", U, None, None, True, "lookup"),
    ("Wanneer was ik voor het laatst in Zorbia?", "nl", "date", "latest", U, None, None, True, "lookup"),
    ("Welke restaurants hebben we in maart 2024 geprobeerd?", "nl", "list", "list_count", "range", D(2024, 3, 1), D(2024, 4, 1), True, "enumeration"),
    ("Waar ligt Zorbia?", "nl", "entity", "single", U, None, None, None, "lookup"),
    ("Is Ilse nog steeds vegetarisch of veganistisch?", "nl", "entity", "single", U, None, None, True, "lookup"),
    ("Leg uit hoe een warmtepomp werkt", "nl", "unknown", "unknown", U, None, None, False, "lookup"),
    ("Hoeveel verdient Ilse?", "nl", "free", "single", U, None, None, True, "lookup"),
    # ---- Spanish
    ("¿Cuándo empezó Ilse a trabajar en Quorvex?", "es", "date", "single", U, None, None, True, "lookup"),
    ("¿Cuánto tiempo vivió Bram en Zorbia?", "es", "duration", "single", U, None, None, True, "lookup"),
    ("¿Cuántas veces visité a mi hermana en 2023?", "es", "count", "list_count", "range", D(2023, 1, 1), D(2024, 1, 1), True, "enumeration"),
    ("Enumera todas las mascotas de Ilse", "es", "list", "list_count", U, None, None, True, "enumeration"),
    ("¿Tiene Bram un perro?", "es", "yesno", "single", U, None, None, True, "lookup"),
    ("¿Dónde vive mi hermana ahora?", "es", "entity", "latest", "current", None, None, True, "lookup"),
    ("¿Cuál era mi dirección a fecha de 4 de marzo de 2021?", "es", "entity", "as_of", "as_of", None, None, True, "as_of"),
    ("Muéstrame el historial de mi trabajo", "es", "unknown", "history", "history", None, None, True, "enumeration"),
    ("¿Quién es mayor que Bram, Ilse o Marta?", "es", "entity", "comparison", U, None, None, True, "lookup"),
    ("¿Qué tienen Ilse y Bram en común?", "es", "entity", "join", U, None, None, True, "lookup"),
    ("¿Cuánto tiempo después de mi mudanza a Zorbia cambié de trabajo?", "es", "duration", "join", U, None, None, True, "lookup"),
    ("¿Cuál es la capital de Freedonia?", "es", "entity", "single", U, None, None, False, "lookup"),
    ("Dime todo lo que sabes sobre Ilse", "es", "list", "list_count", U, None, None, True, "show_all"),
    ("¿Qué comió mi hermana ayer?", "es", "entity", "single", "range", D(2025, 5, 13), D(2025, 5, 14), True, "as_of"),
    ("¿Por qué dejó Bram Quorvex?", "es", "free", "single", U, None, None, True, "lookup"),
    ("¿Cuándo fue la última vez que visité a Ilse?", "es", "date", "latest", U, None, None, True, "lookup"),
    ("¿Dónde queda Zorbia?", "es", "entity", "single", U, None, None, None, "lookup"),
    ("¿Es Ilse vegetariana o vegana?", "es", "entity", "single", U, None, None, True, "lookup"),
    ("Explica cómo funciona una bomba de calor", "es", "unknown", "unknown", U, None, None, False, "lookup"),
    ("¿Qué ha cambiado en el trabajo de Ilse desde 2022?", "es", "entity", "history", "history", D(2022, 1, 1), None, True, "enumeration"),
    ("¿Cuántos años tiene Bram?", "es", "count", "list_count", U, None, None, True, "enumeration"),
]

# fmt: on


@pytest.mark.parametrize("case", CASES, ids=[c[0][:48] for c in CASES])
def test_table(case: tuple) -> None:
    q, lang, atype, shape, vkind, vstart, vend, personal, req = case
    c = build_contract(q, now=NOW, lang_hint=None)
    got = (
        c.language,
        c.answer_type,
        c.shape,
        c.time_view.kind,
        c.time_view.start,
        c.time_view.end,
        c.personal,
        c.request_kind,
    )
    assert got == (lang, atype, shape, vkind, vstart, vend, personal, req), q


def test_table_size() -> None:
    assert len(CASES) >= 80


def test_table_is_deterministic() -> None:
    for q, *_ in CASES:
        a = build_contract(q, now=NOW, lang_hint=None)
        assert a == build_contract(q, now=NOW, lang_hint=None)
        assert render_header(a) == render_header(a)
