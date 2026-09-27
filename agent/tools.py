import ipaddress
import logging
import re
from urllib.parse import urlsplit

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


_TOKEN_SPLIT_RE = re.compile(r"[,\n\s]+")


def _extract_host(value: str) -> "str | None":
    """Extract a bare hostname from a single URL or bare domain/host string."""
    value = value.strip()
    if not value:
        return None
    if "://" in value:
        host = urlsplit(value).hostname
        return host.lower() if host else None
    # Bare domain/host, possibly with a trailing path or :port — strip both.
    host = value.split("/", 1)[0].split(":", 1)[0].strip().lower()
    return host or None


def is_target_in_scope(value: str, scope: list[str]) -> bool:
    """Return True if every host found in *value* is inside authorized scope.

    [FIX-SCOPE-DOMAIN] is_in_scope() above only ever matches raw IP
    addresses found inside a shell command string (os_exec's argument) —
    it has no concept of a domain or hostname at all. pentester.md's own
    "Scope & authorization" section promises the model that "every
    command is checked against target_scope... If a tool call against a
    domain/public IP comes back [SCOPE BLOCK], that's a configuration
    gap" — but subdomain_enum, subdomain_bruteforce, http_get/post/head/
    check, the http_session_* tools, and probe_payloads all take a
    domain/url argument, not a "command" string, so none of them were
    ever actually checked. A pentester-mode agent could brute-force DNS,
    send SQLi/XSS probe payloads, or fetch arbitrary URLs against a
    target with ZERO enforcement, while being told by its own system
    prompt that the runtime "has its back."

    This checks HOSTNAMES (exact match or subdomain of a domain scope
    entry — "api.example.com" is in scope for scope entry "example.com")
    against scope, in addition to falling back to IP/CIDR matching for a
    host that's itself a literal IP (e.g. "http://127.0.0.1/admin").

    *value* may be a single domain/URL or a comma/newline/whitespace
    separated list of them (http_check's `urls` argument takes several at
    once) — every host found must be in scope; this fails CLOSED, so one
    out-of-scope host in a batch blocks the whole call rather than
    silently letting the bad one through alongside legitimate ones.

    See execute_tools() in agent/graph.py for where this is actually
    wired in — deliberately gated to active_persona == "pentester" only,
    since http_probe.py's http_get/post/head/check tools are also bound
    to coder/seo/researcher/standard/orchestrator for ordinary internet
    API use, and target_scope's default (localhost + private ranges) has
    no domain entries at all — applying this check universally would
    silently break normal HTTP calls for every persona that isn't doing a
    pentest engagement.
    """
    if not scope or "*" in scope:
        return True

    tokens = [t for t in _TOKEN_SPLIT_RE.split(value.strip()) if t]
    if not tokens:
        return True

    domain_entries: list[str] = []
    networks: list[ipaddress.IPv4Network] = []
    for entry in scope:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            domain_entries.append(entry.strip().lower())

    for token in tokens:
        host = _extract_host(token)
        if not host:
            continue  # nothing host-like in this token — not a scope concern

        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None

        if ip is not None:
            if not any(ip in net for net in networks):
                logger.warning(
                    "[SCOPE BLOCK] %s is outside authorized scope: %s", host, scope,
                )
                return False
            continue

        if not any(host == d or host.endswith("." + d) for d in domain_entries):
            logger.warning(
                "[SCOPE BLOCK] host '%s' is outside authorized scope: %s", host, scope,
            )
            return False

    return True
