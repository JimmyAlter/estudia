from pathlib import Path

from estudia.moodle.pages import youtube_ids

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moodle"


def test_youtube_ids_from_fixture():
    html = (FIXTURES / "page_video_clase_1.html").read_text("utf-8")
    assert youtube_ids(html) == ["aaaaaaaaaa1"]


def test_youtube_formats_and_entities():
    html = (
        '<a href="https://www.youtube.com/watch?v=aaaaaaaaaaa">1</a>'
        '<a href="https://www.youtube.com/watch?feature=x&amp;v=bbbbbbbbbbb">2</a>'
        '<a href="https://youtu.be/ccccccccccc?t=5">3</a>'
        '<iframe src="https://www.youtube.com/embed/ddddddddddd"></iframe>'
        '{&quot;src&quot;:&quot;https://youtu.be/eeeeeeeeeee&quot;}'
        '<a href="https://youtu.be/aaaaaaaaaaa">repetido</a>'
    )
    assert youtube_ids(html) == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd", "eeeeeeeeeee"]


def test_no_youtube():
    assert youtube_ids("<p>nada</p>") == []
