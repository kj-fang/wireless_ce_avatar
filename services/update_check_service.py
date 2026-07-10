"""Check GitHub for a newer released version of IntelAvatar.

Talks to the public GitHub REST API (no auth required) and compares the
`tag_name` of the latest stable release against `configs.version.__version__`.
Uses only stdlib so it is safe to call from the tray-manager subprocess.
"""

import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.request

from configs.version import __version__

_logger = logging.getLogger('UpdateCheckService')

GITHUB_OWNER = 'kj-fang'
GITHUB_REPO = 'wireless_ce_avatar'
LATEST_RELEASE_URL = (
    f'https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest'
)
RELEASES_PAGE_URL = (
    f'https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest'
)
REQUEST_TIMEOUT = 15  # seconds

# Intel corporate proxy — used as a last resort when neither environment
# variables nor Windows Internet Options define one. Change to '' to disable.
DEFAULT_PROXY = 'http://proxy-dmz.intel.com:912'


def _resolve_proxies() -> dict:
    """Discover HTTP/HTTPS proxy from env vars first, then Windows Internet Options,
    then fall back to `DEFAULT_PROXY` (Intel corp proxy) so the tray works
    out-of-the-box on employee laptops without any setup.
    """
    proxies: dict = {}
    for scheme in ('http', 'https'):
        value = (
            os.environ.get(f'{scheme.upper()}_PROXY')
            or os.environ.get(f'{scheme}_proxy')
        )
        if value:
            proxies[scheme] = value

    if not proxies:
        try:
            system_proxies = urllib.request.getproxies() or {}
        except Exception as err:
            _logger.debug(f'getproxies() failed: {err}')
            system_proxies = {}
        for scheme in ('http', 'https'):
            if system_proxies.get(scheme):
                proxies[scheme] = system_proxies[scheme]

    if not proxies and DEFAULT_PROXY:
        proxies = {'http': DEFAULT_PROXY, 'https': DEFAULT_PROXY}

    return proxies


class UpdateCheckError(Exception):
    """Raised when the update check cannot be completed."""


class UpdateCheckResult:
    __slots__ = (
        'is_latest',
        'current_version',
        'latest_version',
        'release_url',
        'download_url',
    )

    def __init__(self, is_latest, current_version, latest_version,
                 release_url, download_url):
        self.is_latest = is_latest
        self.current_version = current_version
        self.latest_version = latest_version
        self.release_url = release_url
        self.download_url = download_url


def _normalize(version_str: str) -> tuple:
    """Convert 'v1.2.3' / '1.2.3-dev.abc' into a comparable numeric tuple.

    Any pre-release / build suffix after '-' or '+' is dropped; non-numeric
    chunks fall back to 0. This is intentionally lenient so a malformed
    version never crashes the tray.
    """
    cleaned = version_str.strip().lstrip('vV')
    cleaned = re.split(r'[-+]', cleaned, maxsplit=1)[0]
    parts = []
    for chunk in cleaned.split('.'):
        match = re.match(r'\d+', chunk)
        parts.append(int(match.group(0)) if match else 0)
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    return _normalize(latest) > _normalize(current)


def _pick_download_url(assets: list, release_url: str) -> str:
    """Prefer the first .exe asset, else the first asset, else release page."""
    if not assets:
        return release_url
    exe_asset = next(
        (a for a in assets if str(a.get('name', '')).lower().endswith('.exe')),
        None,
    )
    chosen = exe_asset or assets[0]
    return chosen.get('browser_download_url') or release_url


def check_for_update() -> UpdateCheckResult:
    """Fetch the latest release from GitHub and compare against current version.

    Raises UpdateCheckError on network / parsing failures so the caller can
    show a friendly message instead of a stack trace.
    """
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={
            'Accept': 'application/vnd.github+json',
            'User-Agent': f'IntelAvatar/{__version__}',
        },
    )
    proxies = _resolve_proxies()
    if proxies:
        _logger.info(f'Using proxy for update check: {proxies}')
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
    else:
        _logger.info('No proxy configured — attempting direct connection')
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as err:
        raise UpdateCheckError(f'GitHub returned HTTP {err.code}') from err
    except urllib.error.URLError as err:
        reason = getattr(err, 'reason', err)
        hint = ''
        if not proxies:
            hint = (
                '\n\nNo proxy was detected. On Intel corporate networks, set:\n'
                '  HTTPS_PROXY=http://proxy-dmz.intel.com:912\n'
                'or configure the system proxy in Windows Internet Options.'
            )
        raise UpdateCheckError(f'Network error: {reason}{hint}') from err
    except (json.JSONDecodeError, ValueError) as err:
        raise UpdateCheckError(f'Invalid response from GitHub: {err}') from err

    tag = payload.get('tag_name') or ''
    latest_version = tag.lstrip('vV') or 'unknown'
    release_url = payload.get('html_url') or RELEASES_PAGE_URL
    download_url = _pick_download_url(payload.get('assets') or [], release_url)

    try:
        is_latest = not _is_newer(latest_version, __version__)
    except Exception as err:
        raise UpdateCheckError(
            f'Unable to compare versions ({__version__} vs {latest_version}): {err}'
        ) from err

    _logger.info(
        f'Update check | current={__version__} latest={latest_version} '
        f'is_latest={is_latest}'
    )
    return UpdateCheckResult(
        is_latest=is_latest,
        current_version=__version__,
        latest_version=latest_version,
        release_url=release_url,
        download_url=download_url,
    )
