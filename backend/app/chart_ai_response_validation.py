"""Bounded, local-only validation and substitution of chart explanation tags."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from datetime import timezone
from decimal import Decimal

from .chart_ai_validation import ValidatedContext

IMAGE_WARNING = "grafikte alakasız yazı var"
_TAG = re.compile(r"\{[a-z][a-z0-9_]*\}")
_FRAMES = {
    "1m": "bir dakika", "5m": "beş dakika", "15m": "on beş dakika",
    "30m": "otuz dakika", "1h": "bir saat", "4h": "dört saat", "1d": "bir gün",
}
_TECHNICAL = frozenset({
    "rsi", "macd", "tp", "atr", "adx", "mtf", "btc", "eth", "usdt", "btcusdt",
    "ethusdt", "long", "short", "stop", "histogram", "momentum",
})
_ENGLISH = frozenset({
    "the", "and", "is", "of", "with", "at", "a", "an", "in", "on", "to", "for",
    "from", "by", "as", "it", "this", "that", "has", "have", "be", "been", "are",
    "was", "were", "will", "would", "could", "can", "if", "or", "but", "not",
    "its", "which", "while", "than", "even", "toward", "above", "below", "between",
    "price", "level", "levels", "support", "resistance", "current", "shows", "remains",
    "neutral", "direction", "mixed", "strong", "stronger", "risk", "risks", "data",
    "confidence", "volume", "timeframe", "timeframes", "downward", "upward",
})
_TURKISH = frozenset({
    "ve", "ile", "bir", "olarak", "bu", "da", "de", "icin", "ancak", "ise",
    "var", "yok", "daha", "ilk", "ikinci", "iki", "uc", "hem", "veya",
})
_TURKISH_STEMS = (
    "fiyat", "seviye", "destek", "direnc", "grafik", "mevcut", "gorun", "yukari",
    "asagi", "risk", "veri", "belirsiz", "hareket", "guven", "esik", "notr",
    "goster", "sinir", "kalicilik", "altin", "uzerin", "senaryo", "acikla",
)
_TR_NUMBER = (
    r"(?:sifir|sıfır|bir|iki|uc|üç|dort|dört|bes|beş|alti|altı|yedi|sekiz|dokuz|"
    r"on|yirmi|otuz|kirk|kırk|elli|altmis|altmış|yetmis|yetmiş|seksen|doksan|"
    r"yuz|yüz|bin|milyon|milyar|bucuk|buçuk|yarim|yarım|ceyrek|çeyrek)"
)
_EN_NUMBER = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred|thousand|million|billion|half|quarter)"
)
_TR_WRITTEN_NUMBER = re.compile(
    rf"\b{_TR_NUMBER}(?:[\s-]+(?:{_TR_NUMBER}|virgul|nokta|eksi))*"
    r"(?:'?(?:nın|nin|nun|nün|ın|in|un|ün|a|e|da|de|ta|te|dan|den|tan|ten|"
    r"yı|yi|yu|yü|ı|i|u|ü|lık|lik|luk|lük|la|le|dır|dir|dur|dür|tır|tir|tur|tür))?\b"
)
_EN_WRITTEN_NUMBER = re.compile(
    rf"\b{_EN_NUMBER}(?:[\s-]+(?:{_EN_NUMBER}|point|comma|minus))*\b"
)
_FINANCIAL = re.compile(
    r"\b(?:fiyat\w*|giris\w*|stop\w*|hedef\w*|destek\w*|direnc\w*|seviye\w*|"
    r"rsi|macd|adx|atr|hacim\w*|guven\w*|price|entry|target\w*|support|resistance|"
    r"level\w*|volume|confidence|threshold\w*|ihtimal\w*|olasilik\w*|olasilig\w*|"
    r"probability|chance)\b"
)
_INDICATOR = re.compile(r"\b(?:histogram\w*|gosterge\w*)\b")
_COUNT_NOUN = re.compile(
    r"^\s+(?:seviye(?:ye|yi|de|den|yle|nin)?|hedef(?:e|te|ten|le|in)?|"
    r"destek(?:te|ten|le)?|desteg(?:e|in)|direnc(?:e|te|ten|le|in)?|"
    r"gosterge(?:ye|yi|de|den|yle|nin)?|senaryo(?:ya|yu|da|dan|yla|nun)?|"
    r"risk(?:e|te|ten|le|in)?|zaman\s+dilimi(?:ne|ni|nde|nden|yle|nin)?)(?![\w'’‘ʼ-])"
)
_COUNT_NOUN_STEM = re.compile(
    r"^\s+(?:seviye\w*|hedef\w*|deste[kg]\w*|direnc\w*|gosterge\w*|senaryo\w*|risk\w*|zaman\s+dilim\w*)"
)
_COUNT_VALUE_PREFIX = re.compile(
    r"\b(?:fiyat\w*|giris\w*|stop\w*|hedef\w*|deste[kg]\w*|direnc\w*|rsi|macd|adx|atr|"
    r"histogram\w*|gosterge\w*|hacim\w*|guven\w*|deger\w*|esi[kg]\w*|oran\w*|"
    r"ihtimal\w*|olasilik\w*|olasilig\w*|eksi|arti|negatif|pozitif|nokta|virgul)"
    r"\s*(?:(?:deger\w*|seviye\w*|esi[kg]\w*|oran\w*|skor\w*|yaklasik|olarak|ise|eksi|arti)\s*){0,3}"
    r"(?:[:=]\s*)?$"
)
_UNIT = re.compile(
    r"^\s*(?:dolar|lira|kurus|usd|usdt|btc|eth|yuzde|percent\w*|percentage\w*|"
    r"dakika\w*|saat\w*|gun|gunde|gunluk|hafta\w*|minute\w*|hour\w*|day\w*|week\w*)\b"
)
_PROBABILITY = r"(?:ihtimal\w*|olasilik\w*|olasilig\w*)"
_OUTCOME = r"(?:kazan\w*|kazanc\w*|basari\w*|getiri\w*|kar(?:i|in|inin|a|li|lilik)?)"
_QUANTIFIED_PROBABILITY = (
    re.compile(rf"\b{_PROBABILITY}\s*(?:(?:yaklasik|olarak|degeri|orani|skoru|tahmini|icin|en|az|fazla|yukselis|dusus)\s+){{0,3}}"
               r"(?:[:=,–—(-]\s*)?(?P<tag>\{[a-z][a-z0-9_]*\})"),
    re.compile(r"(?P<tag>\{[a-z][a-z0-9_]*\})\s*\)?\s*"
               rf"(?:(?:oraninda|oranla|ile|bir|olan|yukselis|dusus)\s+){{0,3}}{_PROBABILITY}\b"),
)
_BANNED = (
    r"\b(?:satin\s+alma|alim|satis)\s+sinyal\w*\b",
    r"\b(?:emir\w*|emr[ie]\w*|al|sat|ac|gir|bekle|tut|buy|sell|purchase|open|enter|close|increase|decrease)\b",
    r"\b(?:alin|aliniz|almalisin|almali|almayin|alabilirsin|alinmali|alinmalidir|satin|satiniz|satmalisin|satmali|satmayin|satabilirsin|satilmali|satilmalidir|girmelisin|girmeli|girebilirsin|acmalisin|acmali|acabilirsin)\b",
    r"\b(?:tavsiye|oner(?:i|il|iyor|irim|iriz|meli)\w*|recommend\w*)\b",
    r"\b(?:percent\w*|percentage\w*)\b",
    rf"\b{_OUTCOME}\b[^.!?;\n]{{0,60}}\b{_PROBABILITY}\b",
    rf"\b{_PROBABILITY}\b[^.!?;\n]{{0,60}}\b{_OUTCOME}\b",
    r"\b(?:kazanma|basari|getiri)\b.{0,30}\b(?:sans|oran)\w*\b",
    r"\b(?:talimat\w*|instructions?\w*|rol\s+degistir\w*|ignore\s+(?:previous|system|all)|forget\s+(?:previous|all)|system\s+prompt|send\s+orders)\b",
    r"\byeni\s+(?:bir\s+)?(?:giris|stop|hedef|fiyat|destek|direnc)\w*\b",
    r"\b(?:islem|long|short|pozisyon|stop|hedef|giris|satis|alim)\w*\b.{0,40}\b(?:ac|gir|gonder|ver|kapat|baslat|artir|azalt|yukselt|dusur|degistir|ayarla|uygula|uzaklastir|yaklastir|daralt|koy|yap|cek|tasi)(?:in|un|iniz|unuz|yin|yiniz|alim|elim|malisin|melisin|mali|meli|abilirsin|ebilirsin)?\b",
    r"\b(?:giris|stop|hedef)\w*\b.{0,40}\b(?:olsun|olmali)\b",
)
_GUARANTEE = (
    r"\b(?:kesin\w*|garanti\w*|mutlaka)\b.{0,40}\b(?:kazanc\w*|kazan\w*|kar|getiri\w*|basari\w*|yuksel\w*|dus\w*)\b",
    r"\b(?:kazanc\w*|kazan\w*|kar|getiri\w*|basari\w*)\b.{0,40}\b(?:kesin\w*|garanti\w*)\b",
    r"\b(?:guaranteed|certain|sure)\b.{0,30}\b(?:profit\w*|win\w*|return\w*|gain\w*)\b",
)


class ResponseValidationError(ValueError):
    def __init__(self, rule: str):
        super().__init__(rule)
        self.rule = rule


def normalize(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(char)).replace("ı", "i")


def format_number(value: float) -> str:
    result = format(Decimal(str(value)), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def context_tags(context: ValidatedContext) -> dict[str, str]:
    source = context.context
    tags = {
        "{tf_ana}": _FRAMES[source.timeframe],
        "{sembol}": source.symbol,
        "{yakalama_zamani}": source.captured_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "{rsi_30}": "30", "{rsi_50}": "50", "{rsi_70}": "70", "{macd_0}": "0",
    }
    pairs = [
        ("fiyat", source.price), ("guven", source.confidence),
        ("giris", source.levels.entry), ("stop", source.levels.stop_loss),
        ("tp1", source.levels.tp1), ("tp2", source.levels.tp2), ("tp3", source.levels.tp3),
        ("destek", source.levels.support), ("direnc", source.levels.resistance),
        ("rsi", source.indicators.rsi), ("macd", source.indicators.macd),
        ("adx", source.indicators.adx), ("atr", source.indicators.atr),
        ("hacim_orani", source.indicators.volume_ratio),
    ]
    for name, value in pairs:
        if value is not None:
            tags["{" + name + "}"] = format_number(value) + ("/100" if name == "guven" else "")
    for row in source.mtf:
        tags["{tf_" + row.timeframe + "}"] = _FRAMES[row.timeframe]
        if row.available and row.confidence is not None:
            tags["{mtf_" + row.timeframe + "_guven}"] = format_number(row.confidence) + "/100"
    return tags


def mask_tags(text: str, tags: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        if match[0] not in tags:
            raise ResponseValidationError("unknown_or_missing_tag")
        following = text[match.end():match.end() + 1]
        if following in {"'", "’", "‘", "ʼ"} or following.isalpha():
            raise ResponseValidationError("tag_suffix")
        return " "

    masked = _TAG.sub(replace, text)
    if "{" in masked or "}" in masked:
        raise ResponseValidationError("malformed_tag")
    return masked


def is_turkish_text(text: str) -> bool:
    words = [word for word in re.findall(r"[a-z]+", normalize(text)) if word not in _TECHNICAL]
    if not words:
        return False
    english = sum(word in _ENGLISH for word in words)
    turkish = sum(word in _TURKISH or word.startswith(_TURKISH_STEMS) for word in words)
    ratio = english / len(words)
    if (english >= 2 and ratio >= 0.35) or (english >= 4 and ratio >= 0.20 and english > turkish * 2):
        return False
    return turkish > 0 or bool(re.search("[çğıöşüÇĞİÖŞÜ]", text))


def has_written_value(text: str) -> bool:
    # Keep spelling: "öne" is neither English "one" nor Turkish "on" with a suffix.
    source = text.casefold().replace("i\u0307", "i")
    for pattern in (_TR_WRITTEN_NUMBER, _EN_WRITTEN_NUMBER):
        for match in pattern.finditer(source):
            before = normalize(source[max(0, match.start() - 90):match.start()])
            after = normalize(source[match.end():])
            number = normalize(match[0])
            if re.search(r"[\s-]", number):
                return True
            noun = _COUNT_NOUN.match(after)
            if number in {"iki", "uc", "dort"} and noun is not None:
                tail = after[noun.end():]
                case_ending = re.sub(r"\s+", " ", noun[0].strip()) not in {
                    "seviye", "hedef", "destek", "direnc", "gosterge", "senaryo", "risk", "zaman dilimi",
                }
                if ((case_ending or not _COUNT_VALUE_PREFIX.search(before) or re.match(r"^\s+arasinda\b", tail))
                        and not _UNIT.match(tail)):
                    continue
            if number != "bir" and _COUNT_NOUN_STEM.match(after):
                return True
            if _UNIT.match(after):
                return True
            if match[0] == "bir" and re.match(
                r"^\s+(?!olacak\b|olabilir\b|olur\b|nokta\b|virgul\b)[a-z]+", after
            ):
                continue
            if match[0] == "bir" and not _FINANCIAL.search(before):
                continue
            if (_FINANCIAL.search(before) or _FINANCIAL.match(after.lstrip())
                    or re.search(r"\b(?:yuz|bin|milyon|milyar|hundred|thousand|million|billion)\b",
                                 normalize(match[0]))):
                return True
            if _INDICATOR.search(before) or _INDICATOR.match(after.lstrip()):
                if normalize(match[0]).replace("'", "") == "sifirdan" and re.match(
                    r"^\s+(?:yeniden\s+)?(?:hesapla|ciz|olustur|degerlendir|incele)\w*\b", after
                ):
                    continue
                return True
    return False


def has_guarantee(text: str) -> bool:
    for pattern in _GUARANTEE:
        for match in re.finditer(pattern, text, re.DOTALL):
            after = text[match.end():]
            if re.match(r"^\s+(?:yok(?:tur)?|degil(?:dir)?|saglamaz|vermez)(?:\s*[.!?,;]|$)", after):
                continue
            return True
    return False


def validate_template(text: str, tags: Mapping[str, str]) -> None:
    for char in text:
        category = unicodedata.category(char)
        if category in {"Cf", "Cc", "Cs", "Co"} and char not in "\n\t":
            raise ResponseValidationError("hidden_character")
        if category.startswith("L") and not unicodedata.name(char, "").startswith("LATIN"):
            raise ResponseValidationError("unsupported_character")
    masked = mask_tags(text, tags)
    if any(char.isnumeric() for char in masked):
        raise ResponseValidationError("literal_number")
    normalized = normalize(masked)
    if any(char in "%‰‱" for char in masked) or re.search(r"\byuzde(?:lik)?\b", normalized):
        raise ResponseValidationError("percentage")
    # Keep field names, not values, so "{macd} ... sıfıra" retains indicator context.
    written_context = _TAG.sub(
        lambda match: " " + re.sub(r"\d+", "", match[0][1:-1]).replace("_", " ") + " ", text
    )
    if has_written_value(written_context):
        raise ResponseValidationError("written_value")
    if any(
        any(char.isnumeric() for char in tags[match["tag"]])
        for pattern in _QUANTIFIED_PROBABILITY for match in pattern.finditer(normalize(text))
    ):
        raise ResponseValidationError("quantified_probability")
    if has_guarantee(normalized) or any(re.search(pattern, normalized, re.DOTALL) for pattern in _BANNED):
        raise ResponseValidationError("advice_guarantee_or_instruction")
    if not is_turkish_text(masked):
        raise ResponseValidationError("non_turkish_or_unclear")


def substitute_tags(text: str, tags: Mapping[str, str]) -> str:
    validate_template(text, tags)
    return _TAG.sub(lambda match: tags[match[0]], text)
