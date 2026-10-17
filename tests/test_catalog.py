from estudia.catalog import get_site, load_catalog


def test_catalog_has_frba():
    site = get_site("utn-frba")
    assert site.url == "https://aulasvirtuales.frba.utn.edu.ar"
    assert site.login == "browser"
    assert site.university == "UTN"


def test_unknown_site_raises():
    import pytest

    with pytest.raises(KeyError):
        get_site("no-existe")


def test_catalog_ids_unique():
    ids = [s.id for s in load_catalog()]
    assert len(ids) == len(set(ids))
