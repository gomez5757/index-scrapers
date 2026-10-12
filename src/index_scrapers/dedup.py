"""Conservative matching of portal listings, with a shared catalogue as authority.

Area/price tolerances are symmetric and use the smaller value as the base.
Photo confirmation is required even for a sole candidate: missing evidence keeps
ads separate. This trades possible false negatives for fewer false merges and
must be calibrated on the real, independently labelled evaluation sample.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import math
import re
import unicodedata


ALGORITHM_VERSION = "conservative-dhash64-v1"
EARTH_RADIUS_M = 6_371_008.8
HASH_MAX_DISTANCE = 6
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _number(value, *, minimum=0):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= minimum
    )


def _within(left, right, tolerance):
    if not (_number(left, minimum=0) and _number(right, minimum=0)):
        return False
    if min(left, right) <= 0:
        return False
    smaller, larger = sorted((Decimal(str(left)), Decimal(str(right))))
    return larger <= smaller * (Decimal(1) + Decimal(tolerance))


def _district(value):
    if not isinstance(value, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


def _location_valid(prop):
    for field, bound in (("lat", 90), ("lng", 180)):
        value = prop.get(field)
        if value is not None and (not _number(value, minimum=-bound) or value > bound):
            return False
    return True


def _near(left, right):
    if not (_location_valid(left) and _location_valid(right)):
        return False
    coordinates = (left.get("lat"), left.get("lng"), right.get("lat"), right.get("lng"))
    if any(value is None for value in coordinates):
        district = _district(left.get("district"))
        return bool(district) and district == _district(right.get("district"))
    lat1, lng1, lat2, lng2 = map(math.radians, coordinates)
    haversine = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    distance = 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1, max(0, haversine))))
    return distance < 75


def _photo(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", value):
        return None
    number = int(value, 16)
    # Flat/monotone photos and common blank placeholders have no useful dHash.
    return None if number in (0, (1 << 64) - 1) else number


def is_duplicate(left: Mapping, right: Mapping) -> bool:
    """Return a pair decision, without changing either record or fetching photos."""
    left_id, right_id = left.get("id"), right.get("id")
    if not isinstance(left_id, str) or not isinstance(right_id, str) or not left_id or not right_id:
        return False
    if left_id == right_id:
        return True
    left_portal = left.get("portal") or left_id.partition(":")[0]
    right_portal = right.get("portal") or right_id.partition(":")[0]
    if left_portal == right_portal:
        return False
    a, b = left.get("property"), right.get("property")
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        return False
    operation = a.get("operation")
    if not isinstance(operation, str) or operation not in {"rent", "sale"} or operation != b.get("operation"):
        return False
    municipality = a.get("municipality")
    if not isinstance(municipality, str) or not re.fullmatch(r"\d{5}", municipality) or municipality != b.get("municipality"):
        return False
    if a.get("kind") and b.get("kind") and a["kind"] != b["kind"]:
        return False
    if not _near(a, b) or not _within(a.get("area_m2"), b.get("area_m2"), "0.05"):
        return False
    rooms_a, rooms_b = a.get("rooms"), b.get("rooms")
    if not (_number(rooms_a) and _number(rooms_b)) or rooms_a != int(rooms_a) or rooms_b != int(rooms_b) or rooms_a != rooms_b:
        return False
    if not _within(left.get("price"), right.get("price"), "0.10"):
        return False
    photo_a, photo_b = _photo(left.get("photo_hash")), _photo(right.get("photo_hash"))
    return photo_a is not None and photo_b is not None and (photo_a ^ photo_b).bit_count() <= HASH_MAX_DISTANCE


def _new_property_id(candidate):
    identity = candidate.get("id")
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("A new property needs a stable listing id")
    try:
        first_seen = datetime.fromisoformat(candidate["first_seen"].replace("Z", "+00:00"))
        if first_seen.tzinfo is None:
            raise ValueError("first_seen must include a timezone")
        milliseconds = int(first_seen.astimezone(timezone.utc).timestamp() * 1000)
        if not 0 <= milliseconds < (1 << 48):
            raise ValueError("first_seen is outside the ULID timestamp range")
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("A new property needs a valid first_seen timestamp") from exc
    value = (milliseconds << 80) | int.from_bytes(sha256(identity.encode()).digest()[:10])
    encoded = []
    for _ in range(26):
        encoded.append(_ULID_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(encoded))


def assign_property(candidate: Mapping, catalog: Iterable[Mapping]) -> str:
    """Preserve known IDs; join one unambiguous group only on all-member evidence.

    The caller supplies the shared catalogue, including records from other
    runners. No catalogue, listing or property is mutated by this function.
    """
    identity = candidate.get("id")
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("A listing needs a stable id before consulting the catalogue")
    records = list(catalog)
    known_ids = {item["property_id"] for item in records if item.get("id") == candidate.get("id") and item.get("property_id")}
    if len(known_ids) > 1:
        raise ValueError("Catalogue has conflicting property ids for one listing")
    if known_ids:
        return known_ids.pop()
    groups = defaultdict(list)
    for item in records:
        if item.get("property_id") and item.get("status", "active") == "active":
            groups[item["property_id"]].append(item)
    matches = [property_id for property_id, items in groups.items() if all(is_duplicate(candidate, item) for item in items)]
    return matches[0] if len(matches) == 1 else _new_property_id(candidate)


def perceptual_hash(image_bytes: bytes) -> str:
    """Compute a 64-bit difference hash in memory; Pillow is an optional extra."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(BytesIO(image_bytes)) as original:
            original.load()
            image = ImageOps.exif_transpose(original).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = image.load()
            value = 0
            for y in range(8):
                for x in range(8):
                    value = (value << 1) | (pixels[x, y] > pixels[x + 1, y])
            return f"{value:016x}"
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid image data for perceptual hash") from exc
