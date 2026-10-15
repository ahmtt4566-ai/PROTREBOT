import binascii
import builtins
import copy
import io
import json
import logging
import struct
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from app.chart_ai_validation import (
    MACD_DEFINITION,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_EDGE,
    PROVENANCE,
    ChartAiValidationError,
    ChartContext,
    _private_decode,
    validate_context,
    validate_image,
)
from PIL import Image, ImageFile, PngImagePlugin

NOW = datetime(2026, 10, 15, 10, 15, tzinfo=timezone.utc)
PRIVATE = "synthetic-private-data:image/png;base64,DO_NOT_LOG"


def picture(format_="PNG", size=(64, 32), **save_args):
    with Image.new("RGB", size, (18, 31, 45)) as image:
        output = io.BytesIO()
        image.save(output, format=format_, **save_args)
        return output.getvalue()


def png_dimensions(width, height):
    original = picture()
    header = struct.pack(">II", width, height) + original[24:29]
    crc = struct.pack(">I", binascii.crc32(b"IHDR" + header) & 0xFFFFFFFF)
    return original[:16] + header + crc + original[33:]


def payload():
    return {
        "symbol": "BTCUSDT", "timeframe": "15m", "captured_at": NOW.isoformat(),
        "price_updated_at": NOW.isoformat(), "market_updated_at": NOW.isoformat(),
        "price": 60000, "indicators": {"rsi": 52, "macd": -0.8, "adx": 25, "atr": 2, "volume_ratio": 1.2},
        "levels": {"entry": 60000, "stop_loss": 59000, "tp1": 61000, "tp2": 62000, "tp3": 63000,
                   "support": 59500, "resistance": 60500},
        "direction": "LONG", "confidence": 65, "signal": "LONG SETUP",
        "mtf": [{"symbol": "BTCUSDT", "timeframe": "1h", "available": True, "direction": "SHORT",
                 "confidence": 45, "trend": "Karışık", "momentum": "Negatif", "updated_at": NOW.isoformat()}],
    }


def context(data=None, **kwargs):
    return validate_context(data if data is not None else payload(), expected_symbol="BTCUSDT",
                            expected_timeframe="15m", now=NOW, **kwargs)


@pytest.mark.parametrize("format_,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_static_real_formats_are_decoded_reencoded_and_bounded(format_, mime):
    source = picture(format_)
    result = validate_image(source, mime)
    assert result.media_type == "image/jpeg" and result.source_media_type == mime
    assert (result.width, result.height) == (64, 32)
    assert len(result.data) <= MAX_IMAGE_BYTES
    assert repr(source) not in repr(result)
    with Image.open(io.BytesIO(result.data)) as decoded:
        decoded.load()
        assert decoded.format == "JPEG" and decoded.size == (64, 32)


@pytest.mark.parametrize("data", [b"", b"not-an-image", b"\x89PNG\r\n\x1a\n" + b"bad", None, "base64-text"])
def test_empty_corrupt_and_nonbinary_images_are_explicitly_rejected(data, caplog):
    with pytest.raises(ChartAiValidationError) as failure:
        validate_image(data, "image/png")
    assert failure.value.code in {"image_empty", "image_invalid"}
    assert "Chart AI input rejected code=" in caplog.text


@pytest.mark.parametrize("mime", ["image/png", "image/webp"])
def test_fake_mime_does_not_override_decoded_jpeg(mime):
    with pytest.raises(ChartAiValidationError, match="gerçek türü") as failure:
        validate_image(picture("JPEG"), mime)
    assert failure.value.code == "image_mime_mismatch" and failure.value.status_code == 415


@pytest.mark.parametrize("mime", ["image/svg+xml", "image/gif", "application/octet-stream", "IMAGE/PNG"])
def test_unapproved_declared_type_is_rejected(mime):
    with pytest.raises(ChartAiValidationError) as failure:
        validate_image(picture(), mime)
    assert failure.value.code == "image_type_invalid"


@pytest.mark.parametrize("source", [b'<svg xmlns="http://www.w3.org/2000/svg"><script>bad()</script></svg>',
                                   picture("GIF"), b"<html>not a chart</html>"])
def test_other_real_contents_cannot_pose_as_png(source):
    with pytest.raises(ChartAiValidationError):
        validate_image(source, "image/png")


def test_five_mib_is_exact_and_oversized_input_never_reaches_decoder(monkeypatch):
    raw = picture()
    exact = raw + b"\0" * (MAX_IMAGE_BYTES - len(raw))
    assert validate_image(exact, "image/png").width == 64
    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: pytest.fail("must reject before decode"))
    with pytest.raises(ChartAiValidationError) as failure:
        validate_image(exact + b"\0", "image/png")
    assert failure.value.code == "image_size_exceeded" and failure.value.status_code == 413


@pytest.mark.parametrize("width,height", [(4000, 4001), (8192, 8192), (200000, 200000)])
def test_resolution_bomb_rejected_from_header_before_pixel_decode(width, height, monkeypatch):
    data = png_dimensions(width, height)
    monkeypatch.setattr(Image.Image, "load", lambda *_args, **_kwargs: pytest.fail("must reject before pixel decode"))
    with pytest.raises(ChartAiValidationError) as failure:
        validate_image(data, "image/png")
    assert failure.value.code == "image_resolution_exceeded"


def test_exact_sixteen_megapixel_boundary_and_long_edge_resize():
    result = validate_image(picture(size=(4000, 4000)), "image/png")
    assert (result.width, result.height) == (MAX_IMAGE_EDGE, MAX_IMAGE_EDGE)


def test_resize_preserves_aspect_ratio_and_does_not_upscale():
    result = validate_image(picture(size=(3200, 1600)), "image/png")
    assert (result.width, result.height) == (1568, 784)
    small = validate_image(picture(size=(30, 90)), "image/png")
    assert (small.width, small.height) == (30, 90)


@pytest.mark.parametrize("format_,mime", [("PNG", "image/png"), ("WEBP", "image/webp")])
def test_animated_supported_formats_are_rejected(format_, mime):
    with Image.new("RGB", (10, 10), "red") as first, Image.new("RGB", (10, 10), "blue") as second:
        output = io.BytesIO()
        first.save(output, format=format_, save_all=True, append_images=[second], duration=100, loop=0)
    with pytest.raises(ChartAiValidationError) as failure:
        validate_image(output.getvalue(), mime)
    assert failure.value.code == "image_animated"


def test_single_frame_apng_animation_container_is_not_static_png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)

    original = picture()
    frame = struct.pack(">IIIIIHHBB", 0, 64, 32, 0, 0, 1, 10, 0, 0)
    raw = original[:33] + chunk(b"acTL", struct.pack(">II", 1, 0)) + chunk(b"fcTL", frame) + original[33:]
    with Image.open(io.BytesIO(raw)) as decoded:
        assert decoded.n_frames == 1 and decoded.info["loop"] == 0
    with pytest.raises(ChartAiValidationError) as failure:
        validate_image(raw, "image/png")
    assert failure.value.code == "image_animated"


def test_single_frame_animated_webp_container_is_rejected():
    with Image.new("RGB", (10, 10), "red") as first, Image.new("RGB", (10, 10), "blue") as second:
        output = io.BytesIO()
        first.save(output, format="WEBP", save_all=True, append_images=[second], duration=100, loop=0)
    raw = output.getvalue()
    chunks = []
    index = 12
    kept_frame = False
    while index < len(raw):
        size = struct.unpack("<I", raw[index + 4:index + 8])[0]
        end = index + 8 + size + size % 2
        if raw[index:index + 4] != b"ANMF" or not kept_frame:
            chunks.append(raw[index:end])
        if raw[index:index + 4] == b"ANMF":
            kept_frame = True
        index = end
    body = b"WEBP" + b"".join(chunks)
    one_frame = b"RIFF" + struct.pack("<I", len(body)) + body
    with Image.open(io.BytesIO(one_frame)) as decoded:
        assert decoded.n_frames == 1
    with pytest.raises(ChartAiValidationError) as failure:
        validate_image(one_frame, "image/webp")
    assert failure.value.code == "image_animated"


@pytest.mark.parametrize("format_,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_metadata_removed_orientation_preserved_and_private_decoder_logs_suppressed(format_, mime, caplog):
    exif = Image.Exif()
    exif[270] = PRIVATE
    exif[274] = 6
    args = {"exif": exif, "icc_profile": PRIVATE.encode()}
    if format_ == "PNG":
        info = PngImagePlugin.PngInfo()
        info.add_text("Description", PRIVATE)
        args["pnginfo"] = info
    raw = picture(format_, size=(120, 40), **args)
    with caplog.at_level(logging.DEBUG):
        result = validate_image(raw, mime)
    assert (result.width, result.height) == (40, 120)
    with Image.open(io.BytesIO(result.data)) as decoded:
        assert len(decoded.getexif()) == 0
        assert not {"exif", "icc_profile", "comment", "xmp", "Description"} & decoded.info.keys()
    assert PRIVATE.encode() not in result.data
    assert PRIVATE not in caplog.text
    assert repr(raw) not in caplog.text


def test_transparency_is_composited_not_revealed_as_hidden_rgb_pixels():
    with Image.new("RGBA", (10, 10), (0, 0, 0, 0)) as image:
        output = io.BytesIO()
        image.save(output, format="PNG")
    result = validate_image(output.getvalue(), "image/png")
    with Image.open(io.BytesIO(result.data)) as decoded:
        assert decoded.getpixel((5, 5)) == (255, 255, 255)


@pytest.mark.parametrize("format_,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_truncated_real_images_are_not_tolerated(format_, mime):
    raw = picture(format_)
    with pytest.raises(ChartAiValidationError) as failure:
        validate_image(raw[:len(raw) // 2], mime)
    assert failure.value.code == "image_invalid"


def test_relaxed_external_decoder_setting_fails_closed_without_changing_it(monkeypatch):
    raw = picture("JPEG")
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    with pytest.raises(ChartAiValidationError) as failure:
        validate_image(raw, "image/jpeg")
    assert failure.value.code == "image_decoder_unsafe" and failure.value.status_code == 503
    assert ImageFile.LOAD_TRUNCATED_IMAGES is True


def test_image_validation_never_writes_disk_and_restores_library_controls(monkeypatch):
    raw = picture()
    maximum = Image.MAX_IMAGE_PIXELS
    truncated = ImageFile.LOAD_TRUNCATED_IMAGES
    decoder = logging.getLogger("PIL.TiffImagePlugin")
    original_filters = decoder.filters[:]
    original_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        assert not set(mode) & {"w", "a", "+"}, "image must never reach disk"
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    for name in ("NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"):
        monkeypatch.setattr(tempfile, name, lambda *_args, **_kwargs: pytest.fail("no temporary image files"))
    validate_image(raw, "image/png")
    with pytest.raises(ChartAiValidationError):
        validate_image(b"bad-private-data", "image/png")
    assert decoder.filters == original_filters
    assert Image.MAX_IMAGE_PIXELS == maximum and ImageFile.LOAD_TRUNCATED_IMAGES == truncated


def test_private_decoder_logs_do_not_mute_other_threads_and_are_restored(caplog):
    entered = threading.Event()
    release = threading.Event()
    decoder = logging.getLogger("PIL.TiffImagePlugin")
    original_filters = decoder.filters[:]

    def worker():
        with _private_decode():
            decoder.debug(PRIVATE)
            entered.set()
            assert release.wait(5)

    with caplog.at_level(logging.DEBUG), ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        try:
            assert entered.wait(5)
            decoder.debug("unrelated-thread-diagnostic")
        finally:
            release.set()
        future.result(timeout=5)
    assert PRIVATE not in caplog.text and "unrelated-thread-diagnostic" in caplog.text
    assert decoder.filters == original_filters


def test_concurrent_image_decodes_strip_metadata_without_leaking(caplog):
    exif = Image.Exif()
    exif[270] = PRIVATE
    raw = picture("JPEG", exif=exif)
    with caplog.at_level(logging.DEBUG), ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _index: validate_image(raw, "image/jpeg"), range(12)))
    assert len(results) == 12 and all(PRIVATE.encode() not in result.data for result in results)
    assert PRIVATE not in caplog.text


def test_valid_context_is_whitelisted_unverified_and_macd_is_histogram():
    result = context()
    assert result.provenance == PROVENANCE and result.warnings == ()
    text = json.loads(result.to_text())
    assert text["macd_definition"] == MACD_DEFINITION and "histogram" in MACD_DEFINITION
    assert text["context"]["indicators"]["macd"] == -0.8
    assert text["context"]["levels"] == payload()["levels"]
    assert text["context"]["mtf"][0]["direction"] == "SHORT"


@pytest.mark.parametrize("field_name", ["balance", "account", "api_key", "secret", "wallet", "user_id",
                                      "provenance", "isPremium", "unknown"])
@pytest.mark.parametrize("location", ["root", "indicators", "levels", "mtf"])
def test_forbidden_fields_rejected_at_every_depth_without_logging_inputs(field_name, location, caplog):
    data = payload()
    target = data if location == "root" else data["mtf"][0] if location == "mtf" else data[location]
    target[field_name] = PRIVATE
    with pytest.raises(ChartAiValidationError) as failure:
        context(data)
    assert failure.value.code == "context_invalid"
    assert PRIVATE not in caplog.text and PRIVATE not in str(failure.value)


@pytest.mark.parametrize("location", ["root", "mtf"])
def test_wrong_symbol_rejected_even_when_mtf_row_is_unavailable(location):
    data = payload()
    target = data if location == "root" else data["mtf"][0]
    target["symbol"] = "ETHUSDT"
    if location == "mtf":
        target["available"] = False
    with pytest.raises(ChartAiValidationError) as failure:
        context(data)
    assert failure.value.code == "context_symbol_mismatch"


def test_wrong_main_timeframe_is_rejected_but_different_mtf_is_intended():
    data = payload()
    data["timeframe"] = "1h"
    with pytest.raises(ChartAiValidationError) as failure:
        context(data)
    assert failure.value.code == "context_timeframe_mismatch"
    assert context().context.mtf[0].timeframe == "1h"


@pytest.mark.parametrize("field_name", ["captured_at", "price_updated_at", "market_updated_at", "mtf"])
@pytest.mark.parametrize("seconds,stale", [(120, False), (121, True)])
def test_stale_threshold_is_exact_and_warns_instead_of_denial(field_name, seconds, stale):
    data = payload()
    stamp = (NOW - timedelta(seconds=seconds)).isoformat()
    if field_name == "mtf":
        data["mtf"][0]["updated_at"] = stamp
        notice = "mtf_1h_timestamp_stale"
    elif field_name == "captured_at":
        for name in ("captured_at", "price_updated_at", "market_updated_at"):
            data[name] = stamp
        data["mtf"][0]["updated_at"] = stamp
        notice = "capture_stale"
    else:
        data[field_name] = stamp
        notice = "price_timestamp_stale" if field_name == "price_updated_at" else "market_timestamp_stale"
    assert (notice in context(data).warnings) is stale


@pytest.mark.parametrize("field_name", ["captured_at", "price_updated_at", "market_updated_at", "mtf"])
@pytest.mark.parametrize("seconds,rejects", [(30, False), (31, True)])
def test_future_tolerance_is_exact_and_not_a_silent_fallback(field_name, seconds, rejects):
    data = payload()
    stamp = (NOW + timedelta(seconds=seconds)).isoformat()
    if field_name == "mtf":
        data["mtf"][0]["updated_at"] = stamp
    else:
        data[field_name] = stamp
    if rejects:
        with pytest.raises(ChartAiValidationError) as failure:
            context(data)
        assert failure.value.code == "context_future_timestamp"
    else:
        context(data)


def test_data_from_after_old_capture_is_inconsistent_not_just_stale():
    data = payload()
    data["captured_at"] = (NOW - timedelta(minutes=5)).isoformat()
    with pytest.raises(ChartAiValidationError) as failure:
        context(data)
    assert failure.value.code == "context_time_inconsistent"


@pytest.mark.parametrize("value", ["2026-10-15T10:15:00", 1792059300, True, "bad-date", None])
def test_capture_timestamp_must_be_present_and_timezone_aware(value):
    data = payload()
    data["captured_at"] = value
    with pytest.raises(ChartAiValidationError) as failure:
        context(data)
    assert failure.value.code == "context_invalid"


def test_offset_timestamps_are_normalized_to_utc():
    data = payload()
    data["captured_at"] = NOW.astimezone(timezone(timedelta(hours=2))).isoformat()
    assert context(data).context.captured_at == NOW
    assert json.loads(context(data).to_text())["context"]["captured_at"].endswith("Z")


def test_missing_fields_remain_null_with_explicit_warnings_not_invented_values():
    data = {"symbol": "BTCUSDT", "timeframe": "15m", "captured_at": NOW.isoformat()}
    result = context(data)
    assert {"price_missing", "rsi_missing", "macd_missing", "entry_missing", "stop_loss_missing",
            "tp1_missing", "tp2_missing", "tp3_missing", "mtf_missing", "price_timestamp_missing",
            "market_timestamp_missing"} <= set(result.warnings)
    text = json.loads(result.to_text())["context"]
    assert text["price"] is None and text["levels"]["entry"] is None and text["indicators"]["rsi"] is None


@pytest.mark.parametrize("name,value", [("price", 0), ("price", -1), ("price", float("inf")),
                                      ("confidence", float("nan")), ("confidence", 101), ("price", "60000"),
                                      ("price", True), ("direction", "execute order now")])
def test_invalid_numbers_coercions_and_instruction_strings_are_rejected(name, value):
    data = payload()
    data[name] = value
    with pytest.raises(ChartAiValidationError):
        context(data)


def test_zero_indicators_and_neutral_direction_are_valid_not_missing():
    data = payload()
    data["direction"] = "NEUTRAL"
    data["confidence"] = 0
    data["indicators"] = {"rsi": 0, "macd": 0, "adx": 0, "atr": 0, "volume_ratio": 0}
    assert context(data).warnings == ()


def test_duplicate_or_excessive_mtf_rows_and_invalid_timeframes_are_rejected():
    data = payload()
    data["mtf"].append(copy.deepcopy(data["mtf"][0]))
    with pytest.raises(ChartAiValidationError) as failure:
        context(data)
    assert failure.value.code == "context_mtf_duplicate"
    data["mtf"] *= 3
    with pytest.raises(ChartAiValidationError) as failure:
        context(data)
    assert failure.value.code == "context_invalid"
    data = payload()
    data["mtf"][0]["timeframe"] = "30m"
    with pytest.raises(ChartAiValidationError):
        context(data)


def test_browser_warning_codes_preserved_deduplicated_and_free_text_not_allowed():
    data = payload()
    data["data_warnings"] = ["market_stale", "market_stale"]
    assert context(data).warnings == ("market_stale",)
    data["data_warnings"] = [PRIVATE]
    with pytest.raises(ChartAiValidationError):
        context(data)


def test_unsafe_constructed_model_cannot_bypass_validation():
    data = payload()
    data["price"] = float("nan")
    unsafe = ChartContext.model_construct(**data)
    with pytest.raises(ChartAiValidationError):
        context(unsafe)


def test_invalid_server_clock_is_explicit():
    with pytest.raises(ChartAiValidationError) as failure:
        validate_context(payload(), expected_symbol="BTCUSDT", expected_timeframe="15m",
                         now=NOW.replace(tzinfo=None))
    assert failure.value.code == "server_clock_invalid" and failure.value.status_code == 503
