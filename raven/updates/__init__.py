"""The install's own lifecycle: is a newer raven out, and how do we become it.

Release lookup and version keys, the upgrade plan and the detached handoff
(``upgrade``), the startup update nudge (``update_notice``), the beta
channel pointer (``beta_channel``), and the install-integrity record
(``install_guard``). A feature library consumed by the surfaces -- the
browser/importer pattern: the cli renders its answers, the rpc serves
them to the page, and nothing here imports either.
"""
