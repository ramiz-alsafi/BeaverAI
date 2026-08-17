import ipaddress
import logging
import re

logger = logging.getLogger("beaver")

# IPv4: matches dotted-decimal addresses inside word boundaries
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# IPv6: matches the most common full and compressed forms
# Covers ::1, fe80::1, 2001:db8::, full 8-group addresses, and
# IPv4-mapped addresses (::ffff:192.0.2.1).
# Not exhaustive — a dedicated library is used for validation below.
_IPV6_RE = re.compile(
    r"(?:"
    r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"       # full form
    r"|(?:[0-9a-fA-F]{1,4}:)*:(?:[0-9a-fA-F]{1,4}:)*[0-9a-fA-F]{1,4}"  # compressed
    r"|::(?:[fF]{4}:)?(?:\d{1,3}\.){3}\d{1,3}"         # IPv4-mapped
    r"|::"                                               # loopback shorthand
    r")"
)


def is_in_scope(command: str, scope: list[str]) -> bool:
    """Return True if every IP found in command falls within an authorised scope entry.

    Scope entries may be exact IPs ("192.168.1.1") or CIDR ranges ("10.0.0.0/8").
    An empty scope or a scope containing "*" allows all targets.

    Rules
    -----
    - IPv4 addresses are validated against scope networks.
    - IPv6 addresses are always blocked: add IPv6 CIDR support to scope
      entries and extend this function when needed.
    - Strings that look like IPs but are invalid (e.g. 999.0.0.1) are
      treated as OUT of scope — the safe default.
    - Malformed scope entries are logged and skipped (safe: fewer valid
      networks means stricter enforcement, not looser).
    """
    if not scope or "*" in scope:
        return True

    # ── FIX-3: block commands containing IPv6 addresses ───────────────────────
    ipv6_found = _IPV6_RE.findall(command)
    if ipv6_found:
        logger.warning(
            "[SCOPE BLOCK] IPv6 address(es) detected but IPv6 scope is not "
            "supported: %s — command blocked.",
            ipv6_found,
        )
        return False

    found_ips = _IPV4_RE.findall(command)
    if not found_ips:
        return True  # no IP targets — not a scope concern

    # ── Build network list from scope entries ──────────────────────────────────
    networks: list[ipaddress.IPv4Network] = []
    for entry in scope:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            # FIX-1: log instead of silently dropping
            logger.warning(
                "[SCOPE CONFIG] Malformed scope entry ignored: %r — "
                "fix your target_scope to avoid unintended scope widening.",
                entry,
            )

    # ── Validate each found IP ─────────────────────────────────────────────────
    for ip_str in found_ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            # FIX-2: treat unparseable IPs as out of scope (safe default)
            logger.warning(
                "[SCOPE BLOCK] '%s' looks like an IP but failed validation — "
                "blocking as a precaution.",
                ip_str,
            )
            return False

        if not any(ip in net for net in networks):
            logger.warning(
                "[SCOPE BLOCK] %s is outside authorized scope: %s",
                ip_str,
                scope,
            )
            return False

    return True