"""In-memory page double used by replay unit tests. Not a browser."""


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    async def count(self):
        return 1 if self.selector in self.page.known else 0

    async def click(self, timeout=None):
        self.page.timeouts[selector_key(self.selector)] = timeout
        if self.page.take_flaky(self.selector):
            raise RuntimeError(f"Timeout {timeout}ms exceeded while waiting for {self.selector}")
        self.page.clicks.append(("locator", self.selector))

    async def fill(self, value, timeout=None):
        self.page.timeouts[selector_key(self.selector)] = timeout
        self.page.fills.append((self.selector, value))

    async def input_value(self, timeout=None):
        return self.page.input_values.get(self.selector, "")


def selector_key(selector):
    return selector if isinstance(selector, str) else str(selector)


class FakeMouse:
    def __init__(self, page):
        self.page = page

    async def click(self, x, y):
        self.page.clicks.append(("mouse", float(x), float(y)))


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def type(self, text):
        self.page.typed.append(text)

    async def press(self, key):
        self.page.pressed.append(key)


class FakePage:
    def __init__(self, known=None, flaky=None, url="http://127.0.0.1:8765/demo.html"):
        self.known = set(known or [])
        self.clicks = []
        self.fills = []
        self.typed = []
        self.pressed = []
        self.gotos = []
        self.mouse = FakeMouse(self)
        self.keyboard = FakeKeyboard(self)
        self.shots = []          # screenshot formats requested, in order
        self.body_text = "sample page body"
        self.video = None
        self.url = url
        self.closed = False
        self.timeouts = {}       # selector -> last timeout the replay asked for
        self.input_values = {}   # selector -> value returned by input_value()
        # selectors that fail the first N attempts, so a transient-failure retry
        # can be exercised without a browser
        self.flaky = dict(flaky or {})

    def take_flaky(self, selector):
        """Consume one scripted failure for this selector, if any is left."""
        key = selector_key(selector)
        left = int(self.flaky.get(key) or 0)
        if left <= 0:
            return False
        self.flaky[key] = left - 1
        return True

    async def close(self):
        self.closed = True

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append((url, wait_until, timeout))

    # --- additions used by executor-level tests -----------------------------
    async def screenshot(self, type="png", **kwargs):
        """Record what was asked for: 'jpeg' frames vs 'png' step screenshots."""
        self.shots.append(type)
        if type == "jpeg":
            return b"\xff\xd8" + b"j" * 200
        return b"\x89PNG\r\n\x1a\n" + b"p" * 200

    async def inner_text(self, selector):
        return self.body_text


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class BatchFakeBrowser:
    """A browser that hands out a fresh context (and page) per call.

    Batch execution opens one context per origin and closes it again, so the
    double has to count those instead of returning one shared object.
    """

    def __init__(self, harness):
        self.harness = harness
        self.closed = False
        self.contexts = []

    async def new_context(self, **kwargs):
        page = FakePage(known=self.harness.known, flaky=self.harness.flaky)
        self.harness.pages.append(page)
        context = FakeContext(page)
        self.contexts.append(context)
        self.harness.context_kwargs.append(kwargs)
        return context

    async def close(self):
        self.closed = True
        self.harness.closes += 1


class BatchFakePlaywright:
    """Stands in for `async_playwright()` across a whole batch.

    `launches` is the number the batch is judged on: one browser for N recordings
    is the point of batching, so a second launch is a regression.
    """

    def __init__(self, known=None, flaky=None, launch_error=None, relaunch_after=None):
        self.known = set(known or [])
        self.flaky = dict(flaky or {})
        self.launch_error = launch_error
        self.relaunch_after = relaunch_after
        self.launches = 0
        self.closes = 0
        self.pages = []
        self.browsers = []
        self.context_kwargs = []
        self.chromium = self

    async def launch(self, **kwargs):
        self.launches += 1
        if self.launch_error and self.launches == 1:
            raise RuntimeError(self.launch_error)
        browser = BatchFakeBrowser(self)
        self.browsers.append(browser)
        return browser

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.context = FakeContext(page)
        self.closed = False

    async def new_context(self, **kwargs):
        self.kwargs = kwargs
        return self.context

    async def close(self):
        self.closed = True


class FakePlaywright:
    """Stands in for `async_playwright()` so execute_run can run without Chromium."""

    def __init__(self, page):
        self.browser = FakeBrowser(page)
        self.chromium = self
        self.launch_kwargs = None

    async def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False
