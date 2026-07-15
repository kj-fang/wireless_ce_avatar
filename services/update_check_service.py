"""Check GitHub for a newer released version of IntelAvatar.

Reads the `Location` header of `github.com/<owner>/<repo>/releases/latest`
(which redirects to `/releases/tag/<tag_name>`) and compares that tag against
`configs.version.__version__`. This deliberately avoids `api.github.com` so
the check is not affected by the 60-req/hour unauthenticated rate limit that
is shared across all users behind the Intel corporate proxy.

Uses only stdlib so it is safe to call from the tray-manager subprocess.
"""

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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent urllib from following redirects so we can read the Location."""

    def redirect_request(self, *_args, **_kwargs):
        return None


def _fetch_latest_tag(proxies: dict) -> str:
    """Read the latest release tag from github.com's 302 redirect.

    `https://github.com/<owner>/<repo>/releases/latest` redirects to
    `/releases/tag/<tag_name>`. This uses the github.com web frontend (behind
    Fastly/CDN) instead of api.github.com, so it is not affected by the 60/hr
    unauthenticated API rate limit that our corporate proxy exhausts.
    """
    handlers = []
    if proxies:
        handlers.append(urllib.request.ProxyHandler(proxies))
    handlers.append(
        urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)

    request = urllib.request.Request(
        RELEASES_PAGE_URL,
        headers={'User-Agent': f'IntelAvatar/{__version__}'},
        method='HEAD',
    )

    location = ''
    try:
        response = opener.open(request, timeout=REQUEST_TIMEOUT)
    except urllib.error.HTTPError as err:
        if err.code in (301, 302, 303, 307, 308):
            location = err.headers.get('Location', '') or ''
        else:
            raise UpdateCheckError(
                f'GitHub returned HTTP {err.code} for releases page'
            ) from err
    else:
        try:
            location = response.headers.get('Location', '') or response.geturl()
        finally:
            response.close()

    match = re.search(r'/releases/tag/([^/?#\s]+)', location)
    if not match:
        raise UpdateCheckError(
            f'Could not parse a version tag from redirect target: {location!r}'
        )
    return match.group(1)


def check_for_update() -> UpdateCheckResult:
    """Compare the latest GitHub release against the current version.

    Uses github.com's `/releases/latest` HTTP redirect (not api.github.com) so
    the check is immune to the 60-req/hour unauthenticated API rate limit that
    is shared across all users behind the Intel corporate proxy.
    """
    proxies = _resolve_proxies()
    if proxies:
        _logger.info(f'Using proxy for update check: {proxies}')
    else:
        _logger.info('No proxy configured — attempting direct connection')

    try:
        tag = _fetch_latest_tag(proxies)
    except UpdateCheckError:
        raise
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

    latest_version = tag.lstrip('vV') or 'unknown'
    release_url = f'https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tag/{tag}'
    # No API => no assets list; direct users to the release page to download.
    download_url = release_url

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
