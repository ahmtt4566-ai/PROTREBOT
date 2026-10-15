"""In-memory chart input validation, not connected to any endpoint or provider."""
from __future__ import annotations

import io
import json
import logging
import threading
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, NoReturn

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_EDGE = 1568
STALE_AFTER = timedelta(seconds=120)
FUTURE_TOLERANCE = timedelta(seconds=30)
PROVENANCE = "Tarayıcı kaynaklı, doğrulanmamış."
MACD_DEFINITION = "MACD histogramı: MACD çizgisi (EMA12 - EMA26) eksi çizginin EMA9 sinyali."
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_DECODE_LOCK = threading.RLock()

Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
MtfTimeframe = Literal["1m", "5m", "15m", "1h", "4h"]
Direction = Literal["LONG", "SHORT", "NEUTRAL", "BEKLE"]
Signal = Literal["LONG", "SHORT", "NEUTRAL", "BEKLE", "WAIT", "WATCH", "LONG SETUP",
                 "SHORT SETUP", "STRONG LONG", "STRONG SHORT", "NO TRADE", "BUY", "SELL"]
Symbol = Annotated[str, Field(pattern=r"^[A-Z0-9]{2,24}$")]
Number = Annotated[float, Field(ge=-1e15, le=1e15, allow_inf_nan=False)]
Price = Annotated[float, Field(gt=0, le=1e15, allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(ge=0, le=1e15, allow_inf_nan=False)]
Score = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
BrowserWarning = Literal["market_stale", "market_missing", "price_stale", "price_missing", "mtf_missing"]


class ChartAiValidationError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def reject(code: str, message: str, status_code: int = 422) -> NoReturn:
    logger.warning("Chart AI input rejected code=%s", code)
    raise ChartAiValidationError(code, message, status_code)


class _PrivateDecodeLogs(logging.Filter):
    def __init__(self):
        super().__init__()
        self.thread_id = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread != self.thread_id


@contextmanager
def _private_decode():
    # Serialize decoding to bound memory and avoid warning/filter races between workers.
    with _DECODE_LOCK:
        if ImageFile.LOAD_TRUNCATED_IMAGES:
            reject("image_decoder_unsafe", "Görsel doğrulama güvenlik ayarı uygun değil.", 503)
        # Pillow prints raw EXIF at DEBUG; only this decoding thread is muted.
        filter_ = _PrivateDecodeLogs()
        decoders = [logging.getLogger(f"PIL.{name}") for name in
                    ("Image", "PngImagePlugin", "JpegImagePlugin", "WebPImagePlugin", "TiffImagePlugin")]
        for decoder in decoders:
            decoder.addFilter(filter_)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                yield
        finally:
            for decoder in decoders:
                decoder.removeFilter(filter_)


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes = field(repr=False)
    media_type: str
    source_media_type: str
    width: int
    height: int


def _check_image(image: Image.Image, declared_mime: str, data: bytes) -> None:
    actual = IMAGE_TYPES.get(image.format or "")
    if actual is None:
        reject("image_type_invalid", "Yalnız statik PNG, JPEG veya WebP kabul edilir.", 415)
    if actual != declared_mime:
        reject("image_mime_mismatch", "Görselin gerçek türü bildirilen türle uyuşmuyor.", 415)
    if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_IMAGE_PIXELS:
        reject("image_resolution_exceeded", "Görsel en fazla 16 megapiksel olabilir.", 413)
    # One-frame APNG/WebP containers still carry animation markers.
    apng = image.format == "PNG" and "loop" in image.info
    animated_webp = image.format == "WEBP" and data[12:16] == b"VP8X" and len(data) > 20 and data[20] & 2
    if apng or animated_webp or getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
        reject("image_animated", "Animasyonlu görsel kabul edilmez.")


def validate_image(data: bytes, declared_mime: str) -> ValidatedImage:
    if not isinstance(data, bytes) or not data:
        reject("image_empty", "Geçerli bir görsel dosyası gerekli.")
    if len(data) > MAX_IMAGE_BYTES:
        reject("image_size_exceeded", "Görsel en fazla 5 MiB olabilir.", 413)
    if declared_mime not in IMAGE_TYPES.values():
        reject("image_type_invalid", "Yalnız statik PNG, JPEG veya WebP kabul edilir.", 415)
    try:
        with _private_decode():
            with Image.open(io.BytesIO(data), formats=list(IMAGE_TYPES)) as probe:
                _check_image(probe, declared_mime, data)
                probe.verify()
            with Image.open(io.BytesIO(data), formats=list(IMAGE_TYPES)) as decoded:
                _check_image(decoded, declared_mime, data)
                decoded.load()
                oriented = ImageOps.exif_transpose(decoded)
                try:
                    oriented.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
                    with oriented.convert("RGBA") as rgba, Image.new("RGB", rgba.size, "white") as clean:
                        with rgba.getchannel("A") as alpha:
                            clean.paste(rgba, mask=alpha)
                        output = io.BytesIO()
                        clean.save(output, format="JPEG", quality=90)
                        encoded = output.getvalue()
                        width, height = clean.size
                finally:
                    oriented.close()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        reject("image_resolution_exceeded", "Görsel en fazla 16 megapiksel olabilir.", 413)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        if isinstance(exc, ChartAiValidationError):
            raise
        reject("image_invalid", "Görsel bozuk veya desteklenen bir dosya değil.")
    if len(encoded) > MAX_IMAGE_BYTES:
        reject("image_size_exceeded", "İşlenmiş görsel en fazla 5 MiB olabilir.", 413)
    return ValidatedImage(encoded, "image/jpeg", declared_mime, width, height)


class _ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True,
                              revalidate_instances="always")


class ChartIndicators(_ContextModel):
    rsi: Score | None = None
    macd: Number | None = None
    adx: Score | None = None
    atr: Nonnegative | None = None
    volume_ratio: Nonnegative | None = None


class ChartLevels(_ContextModel):
    entry: Price | None = None
    stop_loss: Price | None = None
    tp1: Price | None = None
    tp2: Price | None = None
    tp3: Price | None = None
    support: Price | None = None
    resistance: Price | None = None


class _TimestampModel(_ContextModel):
    @field_validator("captured_at", "price_updated_at", "market_updated_at", "updated_at",
                     mode="before", check_fields=False)
    @classmethod
    def aware_time(cls, value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str) and len(value) <= 40:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("A timezone-aware ISO timestamp is required")
        return value.astimezone(timezone.utc)


class ChartMtfRow(_TimestampModel):
    symbol: Symbol
    timeframe: MtfTimeframe
    available: bool
    direction: Direction | None = None
    confidence: Score | None = None
    trend: Literal["Güçlü yükseliş", "Güçlü düşüş", "Karışık"] | None = None
    momentum: Literal["Pozitif", "Negatif", "Nötr"] | None = None
    updated_at: datetime | None = None


class ChartContext(_TimestampModel):
    symbol: Symbol
    timeframe: Timeframe
    captured_at: datetime
    price_updated_at: datetime | None = None
    market_updated_at: datetime | None = None
    price: Price | None = None
    indicators: ChartIndicators = Field(default_factory=ChartIndicators)
    levels: ChartLevels = Field(default_factory=ChartLevels)
    direction: Direction | None = None
    confidence: Score | None = None
    signal: Signal | None = None
    mtf: list[ChartMtfRow] = Field(default_factory=list, max_length=5)
    data_warnings: list[BrowserWarning] = Field(default_factory=list, max_length=5)


@dataclass(frozen=True)
class ValidatedContext:
    context: ChartContext = field(repr=False)
    warnings: tuple[str, ...]
    provenance: Literal["Tarayıcı kaynaklı, doğrulanmamış."] = PROVENANCE

    def to_text(self) -> str:
        return json.dumps({
            "provenance": self.provenance, "macd_definition": MACD_DEFINITION,
            "warnings": self.warnings, "context": self.context.model_dump(mode="json"),
        }, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def validate_context(value: object, *, expected_symbol: str, expected_timeframe: str,
                     now: datetime | None = None) -> ValidatedContext:
    try:
        context = ChartContext.model_validate(value)
    except ValidationError:
        reject("context_invalid", "Bağlamda yasak alan, geçersiz sayı veya geçersiz tarih var.")
    if context.symbol != expected_symbol:
        reject("context_symbol_mismatch", "Grafik ve veri sembolü uyuşmuyor.")
    if context.timeframe != expected_timeframe:
        reject("context_timeframe_mismatch", "Grafik ve veri zaman dilimi uyuşmuyor.")
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        reject("server_clock_invalid", "Sunucu zamanı doğrulanamadı.", 503)
    notices: list[str] = list(context.data_warnings)

    def check_time(name: str, timestamp: datetime | None) -> None:
        if timestamp is None:
            notices.append(f"{name}_missing")
            return
        if timestamp - now > FUTURE_TOLERANCE:
            reject("context_future_timestamp", "Bağlamda sunucu saatinden ileride tarih var.")
        if timestamp - context.captured_at > FUTURE_TOLERANCE:
            reject("context_time_inconsistent", "Veri tarihi grafik yakalama zamanı ile uyuşmuyor.")
        if now - timestamp > STALE_AFTER:
            notices.append(f"{name}_stale")

    check_time("capture", context.captured_at)
    check_time("price_timestamp", context.price_updated_at)
    check_time("market_timestamp", context.market_updated_at)
    for name in ("price", "direction", "confidence", "signal"):
        if getattr(context, name) is None:
            notices.append(f"{name}_missing")
    for name in ("rsi", "macd"):
        if getattr(context.indicators, name) is None:
            notices.append(f"{name}_missing")
    for name in ("entry", "stop_loss", "tp1", "tp2", "tp3"):
        if getattr(context.levels, name) is None:
            notices.append(f"{name}_missing")
    if not context.mtf:
        notices.append("mtf_missing")
    seen: set[str] = set()
    for row in context.mtf:
        if row.symbol != context.symbol:
            reject("context_symbol_mismatch", "MTF satırı farklı bir sembole ait.")
        if row.timeframe in seen:
            reject("context_mtf_duplicate", "Aynı MTF zaman dilimi birden fazla kez gönderilemez.")
        seen.add(row.timeframe)
        check_time(f"mtf_{row.timeframe}_timestamp", row.updated_at)
        if not row.available or row.direction is None or row.confidence is None:
            notices.append(f"mtf_{row.timeframe}_missing")
    return ValidatedContext(context, tuple(dict.fromkeys(notices)))
