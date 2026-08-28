"""One browser, shared by the agent and the reader.

The agent's tools and the GUI panel drive the same page: a tool that clicks and
a reader who clicks are the same event to Chromium, which is the whole point --
the reader can take over mid-task and the agent sees what they did.
"""

from raven.browser.driver import BrowserUnavailableError, get_browser

__all__ = ["BrowserUnavailableError", "get_browser"]
