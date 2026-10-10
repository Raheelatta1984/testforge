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

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append((url, wait_until, timeout))
