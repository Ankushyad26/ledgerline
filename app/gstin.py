"""GSTIN helpers: format + checksum validation (real Indian GSTIN algorithm)."""
import re

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

STATE_CODES = {
    "07": "Delhi", "09": "Uttar Pradesh", "19": "West Bengal", "24": "Gujarat",
    "27": "Maharashtra", "29": "Karnataka", "33": "Tamil Nadu", "36": "Telangana",
}


def checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        val = _CHARS.index(ch)
        prod = val * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return _CHARS[(36 - total % 36) % 36]


def make_gstin(state: str, pan: str, entity: str = "1") -> str:
    base = f"{state}{pan}{entity}Z"
    return base + checksum_char(base)


def validate(gstin: str | None) -> tuple[bool, str]:
    if not gstin:
        return False, "missing"
    g = gstin.strip().upper().replace(" ", "")
    if not GSTIN_RE.match(g):
        return False, "format invalid (expected 15 chars: 2-digit state + PAN + entity + Z + check)"
    if checksum_char(g[:14]) != g[14]:
        return False, "checksum digit does not match — likely a typo or a fabricated GSTIN"
    return True, "valid format and checksum"


def state_of(gstin: str | None) -> str | None:
    if not gstin or len(gstin) < 2:
        return None
    return gstin[:2]
