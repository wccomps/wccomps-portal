"""The hosts the portal answers on, for settings (kept apart so tests can call it without reloading settings)."""

from urllib.parse import urlsplit


def trusted_origins(base_url: str, canonical_host: str, legacy_hosts: list[str]) -> list[str]:
    """The sorted https origins of the portal's address, its canonical host (if set) and every legacy host.

    An http base_url (local development) adds nothing: production serves https only, and a same-host form needs no
    trusted origin.
    """
    hosts = {canonical_host, *legacy_hosts} - {""}
    address = urlsplit(base_url)
    if address.scheme == "https" and address.netloc:
        hosts.add(address.netloc)
    return sorted(f"https://{host}" for host in hosts)
