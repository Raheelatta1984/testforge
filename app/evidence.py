"""Capture each step once; logs and PDFs reference the same small JPEG."""
from pathlib import Path


async def capture_once(page, directory, index, sensitive=()):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = f'step-{index}.jpg'
    target = directory / name
    if target.exists():
        return name
    masks = []
    values = [str(v) for v in sensitive if v is not None and str(v)]
    frames = list(getattr(page, 'frames', []))
    if len(frames) > 10:
        raise ValueError('Too many frames to mask safely; no evidence screenshot captured')
    for frame in frames:
        if values:
            await frame.evaluate("""values => { for (const el of document.querySelectorAll('input,textarea')) {
                if (values.some(v => el.value && el.value.includes(v))) el.setAttribute('data-tf-secret','1');
            }}""", values)
        masks.append(frame.locator('input[type="password"], [data-tf-secret]'))
        masks.extend(frame.get_by_text(value, exact=False) for value in values)
    # No full-page capture, device-scale multiplication, second encoding or retry.
    data = await page.screenshot(type='jpeg', quality=35, scale='css', animations='disabled', mask=masks)
    if len(data) > 512 * 1024:
        raise ValueError('Screenshot exceeds 512 KiB evidence cap')
    target.write_bytes(data)
    return name


def callback(directory, sensitive=()):
    async def capture(page, index, values):
        name = await capture_once(page, directory, index, [*sensitive, *values.values()])
        return f'/api/runs/screenshot/{Path(directory).name}/{name}'
    return capture
