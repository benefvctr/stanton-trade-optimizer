"""OCR engines: Windows' built-in OCR first, Tesseract only as a fallback."""
from __future__ import annotations

import asyncio
import shutil

from PIL import Image


class OcrUnavailable(Exception):
    """No usable OCR engine: winrt packages or an OCR language missing, and no Tesseract."""

    def __init__(self, message: str, reason: str = "other"):
        super().__init__(message)
        self.reason = reason   # "path_too_long", "no_language", "not_installed" or "other"


class WindowsOcr:
    """Windows.Media.Ocr through PyWinRT."""

    name = "windows"

    def __init__(self) -> None:
        try:
            from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.storage.streams import Buffer
        except ImportError as exc:
            if "too long" in str(exc):
                # Windows won't load the OCR DLLs once their full path passes 260 characters.
                raise OcrUnavailable("the app's folder path is too long for Windows to load its OCR files; "
                                     r"move the folder somewhere shorter, like C:\Games\Trade Optimizer",
                                     "path_too_long") from exc
            raise OcrUnavailable(f"winrt OCR packages are not installed: {exc}", "not_installed") from exc
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise OcrUnavailable("Windows has no OCR language installed for this user", "no_language")
        self._engine = engine
        self._bitmap_type = SoftwareBitmap
        self._bgra8 = BitmapPixelFormat.BGRA8
        self._buffer_type = Buffer

    def _bitmap(self, image: Image.Image):
        image = image.convert("RGBA")
        r, g, b, a = image.split()
        data = Image.merge("RGBA", (b, g, r, a)).tobytes()
        buf = self._buffer_type(len(data))
        buf.length = len(data)
        memoryview(buf)[:] = data
        return self._bitmap_type.create_copy_from_buffer(buf, self._bgra8, image.width, image.height)

    async def _recognize(self, image: Image.Image):
        return await self._engine.recognize_async(self._bitmap(image))

    def read(self, image: Image.Image) -> str:
        result = asyncio.run(self._recognize(image))
        return "\n".join(" ".join(w.text for w in line.words) for line in result.lines)

    def words(self, image: Image.Image) -> list[tuple[str, float, float, float, float]]:
        """(text, x, y, width, height) for every word, in image pixels."""
        result = asyncio.run(self._recognize(image))
        return [(w.text, w.bounding_rect.x, w.bounding_rect.y, w.bounding_rect.width, w.bounding_rect.height)
                for line in result.lines for w in line.words]

    def lines(self, image: Image.Image) -> list[tuple[str, float, float, float, float]]:
        """(text, x, y, width, height) for every line, the box spanning its words."""
        result = asyncio.run(self._recognize(image))
        out = []
        for line in result.lines:
            rects = [w.bounding_rect for w in line.words]
            if not rects:
                continue
            x0, y0 = min(r.x for r in rects), min(r.y for r in rects)
            x1, y1 = max(r.x + r.width for r in rects), max(r.y + r.height for r in rects)
            out.append((line.text, x0, y0, x1 - x0, y1 - y0))
        return out


class TesseractOcr:
    name = "tesseract"

    def __init__(self, cmd: str = "") -> None:
        try:
            import pytesseract
        except ImportError as exc:
            raise OcrUnavailable("pytesseract is not installed (pip install pytesseract)") from exc
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        elif not shutil.which("tesseract"):
            raise OcrUnavailable("tesseract.exe not found; set [ocr] tesseract_cmd in config.toml")
        self._pt = pytesseract

    def read(self, image: Image.Image) -> str:
        return self._pt.image_to_string(image, config="--psm 6")

    def words(self, image: Image.Image) -> list[tuple[str, float, float, float, float]]:
        d = self._pt.image_to_data(image, output_type=self._pt.Output.DICT)
        return [(t, d["left"][i], d["top"][i], d["width"][i], d["height"][i])
                for i, t in enumerate(d["text"]) if t.strip()]


def make_engine(kind: str = "auto", tesseract_cmd: str = ""):
    errors = []
    order = {"windows": [WindowsOcr], "tesseract": [TesseractOcr]}.get(kind, [WindowsOcr, TesseractOcr])
    for cls in order:
        try:
            return cls(tesseract_cmd) if cls is TesseractOcr else cls()
        except OcrUnavailable as e:
            errors.append(str(e))
    raise OcrUnavailable("; ".join(errors))
