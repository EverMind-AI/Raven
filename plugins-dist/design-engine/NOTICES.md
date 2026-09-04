# Third-party notices for the design-engine distribution

## PyMuPDF (AGPL-3.0)

The `render` extra depends on PyMuPDF (`fitz`), the only AGPL-3.0 dependency
in this distribution (Artifex also sells a commercial licence). It is
imported, not modified and not redistributed: this wheel ships no PyMuPDF
code, and the obligation attaches to distribution of PyMuPDF itself or to
offering it as part of a network service. Page rasterisation for previews
deliberately walks pdfium (`pypdfium2`, Apache-2.0); PyMuPDF is used for
reading PDF sources where layout fidelity matters. The obligation stays on
this wheel's dependency face and never reaches the host raven wheel, whose
dependency set keeps zero `fitz` (design verdict, engine-seat ruling; the
ppt-engine D10 precedent).

Deployers who expose this engine as part of a publicly offered network
service should review AGPL-3.0 section 13 for their own obligations.

## Playwright (Apache-2.0)

Browser rendering uses Playwright and its managed Chromium; both are fetched
by the deployer (`playwright install chromium`), never redistributed here.
