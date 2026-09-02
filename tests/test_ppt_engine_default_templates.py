from pathlib import Path

import pytest

pytest.importorskip("pptx")

_TEMPLATES = Path(__file__).resolve().parents[1] / "plugins-dist" / "ppt-engine" / "raven_ppt" / "assets" / "templates"
_needs_templates = pytest.mark.skipif(
    not any(_TEMPLATES.glob("*.pptx")),
    reason="the template payload is fetched, not tracked; see plugins-dist/ppt-engine/templates.manifest.json",
)
pytestmark = _needs_templates

from raven_ppt.contracts import Project  # noqa: E402
from raven_ppt.services.template import (  # noqa: E402
    default_template_catalog,
    house_style,
)
from raven_ppt.services.template.bind import bind  # noqa: E402
from raven_ppt.services.template.defaults import DEFAULT_TEMPLATES  # noqa: E402
from raven_ppt.services.template.menu import menu, roles  # noqa: E402


def test_every_bundled_template_is_light_present_and_named_once() -> None:
    """Counted off the declaration rather than written down here.

    A number in this test said ten while twelve shipped, because the swap that
    changed the set had no reason to come here. What the catalogue owes is that
    every entry resolves, none is a duplicate, and the promise the prompt makes
    about them -- these are the light ones -- is true of each.
    """
    catalog = default_template_catalog()
    declared = [template for template in DEFAULT_TEMPLATES if template.path.is_file()]

    assert catalog == tuple(declared), "the catalogue is what is declared and on disk"
    assert catalog, "a checkout with no bundled template leaves a task with none to offer"
    assert all("light" in template.tags for template in catalog)
    assert len({template.filename for template in catalog}) == len(catalog)


@pytest.mark.parametrize("index", range(10))
def test_each_bundled_template_binds_and_exposes_house_style(tmp_path: Path, index: int) -> None:
    template = default_template_catalog()[index]
    project = Project(tmp_path, f"template_{index}")

    bound = bind(template.path, project)

    assert bound is not None
    assert bound.example_pages > 0
    entries = menu(bound.source)
    named = roles(entries)
    assert named.get("cover") == 1
    assert "closing" in named
    assert house_style(bound.source, entries) is not None
