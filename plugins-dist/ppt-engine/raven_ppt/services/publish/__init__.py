"""Getting a finished deck to where the user asked for it, or refusing to.

Two things live here because they are one decision. The predecessor had two
publish paths: the schema route verified a digest and wrote atomically through
the state store, and the script route called `shutil.copy2` straight into the
export directory, bypassing all of it. A run then found a third path -- it left
the tool entirely, ran its own script and copied the result itself -- and
delivered a deck that no gate had ever seen.

That is the shape of the risk, so fail-closed is structural here rather than
remembered: `publish` takes the findings and raises on a blocking one. There is
no way to call it without having asked.
"""

from raven_ppt.services.publish.deliver import PublishRefusedError, Staged, publish, stage
from raven_ppt.services.publish.provenance import strip_vendor_marks

__all__ = ["PublishRefusedError", "Staged", "publish", "stage", "strip_vendor_marks"]
