from pathlib import Path

import pytest

pytest.importorskip("pptx")

from raven.ppt.contracts import Project  # noqa: E402
from raven.ppt.services.template import (  # noqa: E402
    default_template_catalog,
    house_style,
)
from raven.ppt.services.template.bind import bind  # noqa: E402
from raven.ppt.services.template.menu import menu, roles  # noqa: E402


def test_bundled_catalog_has_ten_light_templates() -> None:
    catalog = default_template_catalog()

    assert len(catalog) == 10
    assert all("light" in template.tags for template in catalog)
    assert all(template.path.is_file() for template in catalog)
    assert len({template.filename for template in catalog}) == 10


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
