"""In-memory page double used by replay unit tests. Not a browser."""


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    async def count(self):
        return 1 if self.selector in self.page.known else 0

    async def click(self, timeout=None):
        self.page.clicks.append(("locator", self.selector))

    async def fill(self, value, timeout=None):
        self.page.fills.append((self.selector, value))


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
    def __init__(self, known=None):
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
