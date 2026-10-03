import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "correction", Path(__file__).parents[1] / "scripts/correct_post26759.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_replacement_preserves_every_other_character():
    old = '<p>Prefix</p><p><a href="source">Story' + mod.OLD + " Remaining text.</p>"
    new = mod.replacement(old)
    assert new.replace(mod.NEW, mod.OLD) == old
    assert mod.replacement(new) == new


@pytest.mark.parametrize("content", ["Other text", mod.OLD * 2, mod.OLD + mod.NEW])
def test_ambiguous_preconditions_stop(content):
    with pytest.raises(ValueError):
        mod.replacement(content)


def test_wrong_host_stops_before_any_request():
    session = Mock()
    with pytest.raises(ValueError):
        mod.correct("https://other.example", session)
    assert not session.get.called and not session.post.called


def test_verified_exact_correction_only_sends_content():
    before = {
        "id": mod.POST_ID,
        "link": mod.LINK,
        "status": "publish",
        "title": {"raw": "Title"},
        "content": {"raw": "prefix" + mod.OLD + "suffix"},
    }
    after = {**before, "content": {"raw": "prefix" + mod.NEW + "suffix"}}
    session = Mock()
    session.get.side_effect = [Mock(json=lambda: before), Mock(json=lambda: after)]
    public_get = Mock(return_value=Mock(json=lambda: {**after, "content": {"rendered": mod.NEW}}))
    assert mod.correct(mod.BASE, session, public_get)["verified"]
    session.post.assert_called_once_with(
        mod.BASE + "/wp-json/wp/v2/posts/26759",
        json={"content": after["content"]["raw"]},
        timeout=(10, 30),
    )
