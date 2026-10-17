"""Catálogo de campus Moodle soportados."""

import json
from dataclasses import dataclass
from importlib import resources
from typing import Literal


@dataclass(frozen=True)
class Site:
    id: str
    name: str
    university: str
    url: str
    login: Literal["password", "browser", "embedded"]


def load_catalog() -> list[Site]:
    texto = resources.files(__package__).joinpath("facultades.json").read_text("utf-8")
    return [Site(**entrada) for entrada in json.loads(texto)]


def get_site(site_id: str) -> Site:
    for site in load_catalog():
        if site.id == site_id:
            return site
    raise KeyError(f"Campus desconocido: {site_id}")
