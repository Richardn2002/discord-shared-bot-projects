"""Link Scrubber - canonicalize links in Discord messages, drop tracking.

The deployed platform project is ``link-sanitizer`` (nickname "Link
Scrubber"); this file is the source of truth for its draft.

What a message gets
-------------------
A link is rewritten only when the rewrite is provably an improvement:

* a short-link domain (b23.tv, nico.ms) is expanded with one HTTPS request,
  then the target is canonicalized by the rule for its host;
* a known site is rewritten offline - tracking parameters removed, host and
  path canonicalized.  Twitter/X status links are rewritten onto the
  fxtwitter.com mirror the server actually uses, because x.com links do not
  embed on Discord; username-less ``/i/status/<id>`` links have their screen
  name resolved first (one request, KV-cached);
* any other host is only touched when it carries an unambiguous tracking
  parameter (``?utm_*``, ``?fbclid``, ...).

One reply per message, only when at least one link actually changed; a message
whose links are already canonical gets nothing.  At most ``MAX_LINKS`` links
are mentioned; short links and screen names are cached in the KV store, and
every network step runs under a hard deadline (see BUDGETS).

A Spotify *track* link additionally gets a music bridge: the track title and
artists are read from Spotify's public embed page, the best matching video is
found on YouTube, and one line is appended to the reply:

    https://open.spotify.com/track/<id>
    YouTube: https://www.youtube.com/watch?v=<id>

The bridge is cached per track, degrades to silence, and never runs when the
message already carries a YouTube link.

Extending
---------
Two tables drive the offline rules; both hold objects, so they sit below the
functions they name.  Append a rule to ``SHORTENERS`` (a domain that only
redirects) or to ``SITES`` (a domain whose canonical form is computable),
then add a harness case in ``dev/harness.py``.  ``GLOBAL_TRACKING`` already
covers the common ``?utm_*`` / ``?fbclid`` cloud on every other domain.
"""

import http.client
import json
import re
import socket
import time
import unicodedata
from urllib.parse import (
    parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# --- budgets: the platform kills a handler at 5 s, so stay under it ---------
MAX_LINKS = 3            # most links named in one reply
MAX_HOPS = 3             # most shortener redirects followed per link
TOTAL_BUDGET = 2.0       # seconds for the link pass
BRIDGE_BUDGET = 3.4      # seconds for the Spotify -> YouTube bridge
ZHIHU_BUDGET = 2.5       # seconds for one zhihu preview fetch
BILI_BUDGET = 2.6        # seconds for the dynamic API plus the page fallback
HANDLER_CAP = 4.3        # absolute ceiling for everything a handler does
REQUEST_TIMEOUT = 1.5    # seconds per request, capped by the budget

CACHE_KEY = "resolved"       # shortener hop -> target
TWITTER_KEY = "twitter"      # status id -> screen name ("" = none found)
YOUTUBE_KEY = "youtube2"     # v2: the ranker changed, v1 picks are stale decisions
CACHE_TTL = 30 * 86400
MISS_TTL = 2 * 86400
CACHE_MAX = 400

SPOTIFY_EMBED = "https://open.spotify.com/embed/track/%s"
# Zhihu serves browser and crawler user agents a 403 "abnormal traffic"
# page; the Android app client gets a server-rendered page with the same
# metadata, keyless and unsigned.  Verified against 128 archived answers.
ZHIHU_UA = ("osee2unifiedRelease/10191 osee2unifiedReleaseVersion/10.19.1 "
            "ZhihuHybrid Android com.zhihu.android/Futureve-Android")
# Bilibili's dynamic API is the mirror image of zhihu's: a plain desktop
# browser UA plus a Referer gets code=0 with no cookie and no signature, while
# the app UA is risk-blocked (-352).
BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
BILI_APP_UA = ("Mozilla/5.0 BiliDroid/7.62.0 (bbcallen@gmail.com) os/android "
               "model/Pixel build/PS7322 innerVer/7620010 osVer/13 network/2")
BILI_HEADERS = {"Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9"}
BILI_DYNAMIC_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id=%s"
BILI_ARTICLE_API = "https://api.bilibili.com/x/article/viewinfo?id=%s"
BILI_DYNAMIC_HOSTS = ("t.bilibili.com",)
BILI_KEY = "bilibili"
BILI_TTL = 7 * 86400
BILI_COLOR = 0xFB7299       # bilibili pink
ZHIHU_PINS_API = "https://www.zhihu.com/api/v4/pins/%s"
ZHIHU_HOSTS = ("zhihu.com", "zhuanlan.zhihu.com")
ZHIHU_KEY = "zhihu"
ZHIHU_TTL = 7 * 86400      # answers get edited, but not often
ZHIHU_COLOR = 0x0084FF     # zhihu blue
YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query=%s"
YOUTUBE_API = ("https://www.youtube.com/youtubei/v1/search"
               "?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8&prettyPrint=false")
YOUTUBE_CLIENT = {"clientName": "WEB", "clientVersion": "2.20240101.00.00", "hl": "en"}
YOUTUBE_COOKIE = {"Cookie": "SOCS=CAI"}  # skips the EU consent interstitial

# Redirect domains: one GET, headers only, first Location wins.  The target is
# then run through SITES / GLOBAL_TRACKING.  Adding one: (name, (hosts...)).
SHORTENERS = (
    ("bilibili", ("b23.tv", "bili2233.cn", "bili22.cn", "bili33.cn")),
    ("nicovideo", ("nico.ms",)),
)

# Unambiguous click-tracking parameters, dropped on every host.
GLOBAL_TRACKING = frozenset((
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "twclid", "ttclid",
    "igsh", "igshid", "mc_cid", "mc_eid", "_openstat", "vero_id", "li_fat_id",
    "mkt_tok", "epik",
))
GLOBAL_PREFIXES = ("utm_",)

# Bilibili's own junk; kept apart from GLOBAL_TRACKING so that a /video/ link
# can be reduced to the parameters that are content (p, t) and nothing else.
BILI_JUNK = frozenset((
    "spm_id_from", "spmid", "from_spmid", "from_source", "from", "seid",
    "msource", "navhide", "bsource", "buvid", "buvid_fr", "buvid3", "buvid4",
    "b_nut", "vd_source", "mid", "share_source", "share_medium", "share_plat",
    "share_session_id", "share_tag", "share_from", "share_times", "timestamp",
    "unique_k", "up_id", "plat_id", "is_story_h5", "broadcast_type",
    "is_room_feed", "live_from", "visit_id", "sourceFrom",
    # the app's share router hint ("-Arouter=story"), seen on b23.tv expansions
    "-arouter",
))

TWITTER_HOSTS = ("x.com", "twitter.com", "mobile.twitter.com", "m.twitter.com",
                 "fxtwitter.com", "fixupx.com", "vxtwitter.com", "twittpr.com")
TWITTER_SOURCE = ("x.com", "twitter.com", "mobile.twitter.com", "m.twitter.com")
TWITTER_MIRROR = "fxtwitter.com"   # where x.com links are mirrored to
SPOTIFY_JUNK = frozenset(("si",))
YOUTUBE_JUNK = frozenset(("si", "feature", "pp", "app", "si_inferred"))
STEAM_JUNK = frozenset(("snr",))
NICO_JUNK = frozenset(("ref", "thumb_watch", "tracking_id"))
PIXIV_LANGS = ("en", "ja", "zh", "zh-tw", "ko")   # pixiv's locale prefixes

_URL_RE = re.compile(r"https?://[^\s<>\"'`|]+", re.IGNORECASE)
_BV_RE = re.compile(r"^(?:BV[0-9A-Za-z]{8,}|av[0-9]+)$", re.IGNORECASE)
_SPOTIFY_ID_RE = re.compile(r"^[0-9A-Za-z]{16,}$")
_TWEET_URL_RE = re.compile(
    r"https?://(?:mobile\.|m\.)?(?:twitter|x)\.com/"
    r"(?P<user>[0-9A-Za-z_]+)/status(?:es)?/(?P<id>[0-9]+)", re.IGNORECASE)
_SCREEN_NAME_RE = re.compile(r'"screen_name":"([0-9A-Za-z_]+)"')
_YT_PAIR_RE = re.compile(
    r'"videoRenderer":\{"videoId":"([\w-]{11})"'
    r'.{0,1200}?"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', re.S)
_YT_OWNER_RE = re.compile(
    r'"ownerText":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"')
_YT_LENGTH_RE = re.compile(r'"lengthText":.{0,400}?"simpleText":"([\d:]+)"', re.S)
_YT_CORRECTED_RE = re.compile(r'"correctedQuery":\{"runs":\[(.*?)\]', re.S)


# ---------------------------------------------------------------------------
# HTTP: IPv4-pinned, headers or capped body, never a byte more than needed
# ---------------------------------------------------------------------------

class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that only ever dials IPv4.

    The worker's resolver also returns AAAA records the sandbox cannot use,
    and letting socket.create_connection() fail over to a usable address costs
    ~16 s per call - more than the whole handler budget.  Resolving IPv4 up
    front and connecting straight to that address takes milliseconds.
    """

    def connect(self):
        entries = socket.getaddrinfo(
            self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
        )
        if not entries:
            http.client.HTTPSConnection.connect(self)
            return
        family, kind, proto, _name, address = entries[0]
        self.sock = socket.socket(family, kind, proto)
        self.sock.settimeout(self.timeout)
        self.sock.connect(address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _IPv4HTTPConnection(http.client.HTTPConnection):
    """The plain-http twin of `_IPv4HTTPSConnection`."""

    def connect(self):
        entries = socket.getaddrinfo(
            self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
        )
        if not entries:
            http.client.HTTPConnection.connect(self)
            return
        family, kind, proto, _name, address = entries[0]
        self.sock = socket.socket(family, kind, proto)
        self.sock.settimeout(self.timeout)
        self.sock.connect(address)
        if self._tunnel_host:
            self._tunnel()


def _connection(url, timeout):
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not host or scheme not in ("http", "https"):
        return None, None
    https = scheme == "https"
    factory = _IPv4HTTPSConnection if https else _IPv4HTTPConnection
    port = parts.port or (443 if https else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return factory(host, port, timeout=timeout), path


def _location(url, timeout):
    """Return the Location of a redirect response, or None.

    One GET, the response body is never read - that is the whole speed trick
    for short links.
    """
    connection, path = _connection(url, timeout)
    if connection is None:
        return None
    try:
        connection.request("GET", path, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,*/*;q=0.8",
            "Accept-Language": "en;q=0.8",
            "Connection": "close",
        })
        response = connection.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            location = response.getheader("Location")
            if location:
                return urljoin(url, location)
        return None
    finally:
        connection.close()


def _get_body(url, timeout, headers=None, limit=300_000, method="GET", body=None, hops=3):
    """Return the first `limit` bytes of a 200 response body, or None.

    A GET follows up to `hops` redirects - publish.twitter.com answers its
    oEmbed endpoint with a 301 - while a POST never does.  `timeout` is the
    window for the whole chain, not for a single hop.
    """
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    request_headers.update(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    end = time.monotonic() + timeout  # one window for the whole redirect chain
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0.2:
            return None
        connection, path = _connection(url, remaining)
        if connection is None:
            return None
        location = None
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
            elif response.status == 200:
                return response.read(limit)
            else:
                log("GET %s -> HTTP %s" % (url.split("?")[0], response.status))
                return None
        finally:
            connection.close()
        if not location or method != "GET" or hops <= 0:
            return None
        url = urljoin(url, location)
        hops -= 1


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _tidy(raw):
    """A URL lifted out of prose: trim punctuation that belongs to the prose."""
    url = raw.strip()
    while url and url[-1] in ".,;:!?\"":
        url = url[:-1]
    for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
        while url.endswith(closer) and url.count(closer) > url.count(opener):
            url = url[:-1]
    return url


def _host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_web_url(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme or "").lower() in ("http", "https") and bool(parts.hostname)


def _host_match(host, suffixes):
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _rule(rules, host):
    for rule in rules:
        if _host_match(host, rule[1]):
            return rule
    return None


def _build(scheme, host, path, pairs, fragment=""):
    return urlunsplit((scheme, host, path, urlencode(pairs), fragment))


def _strip_tracking(url, drop, prefixes=()):
    """Drop `drop` (and `prefixes`) parameters; return the URL unchanged if none."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [pair for pair in pairs
            if pair[0].lower() not in drop
            and not (prefixes and pair[0].lower().startswith(prefixes))]
    if len(kept) == len(pairs):
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), parts.fragment))


def _drop_all(junk):
    """drop-set for a site rule: its own junk plus the global families."""
    return frozenset(junk) | GLOBAL_TRACKING


def _walk(node):
    """Yield every dict/list in a parsed JSON document, depth first."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            yield current
            stack.extend(current)


# ---------------------------------------------------------------------------
# site rules
# ---------------------------------------------------------------------------

def _youtube(url):
    """youtube.com -> https://www.youtube.com/watch?v=<id>...

    Keeps the parameters that are content (``list``, ``t``, ``index``), drops
    the rest of YouTube's share junk (``si``, ``feature``, ``pp``, ...).  A
    youtu.be link is not rewritten: Discord renders it natively, so it is
    either left alone or only stripped of its tracking parameter.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    query = parse_qsl(parts.query, keep_blank_values=True)
    values = dict(query)
    keep = [(key, values[key]) for key in ("list", "t", "index") if key in values]
    segments = [segment for segment in (parts.path or "").split("/") if segment]
    if host in ("youtu.be", "www.youtu.be") and segments:
        # youtu.be is what Discord renders natively, so the watch form is not
        # an upgrade: a bare link is left alone entirely, and a share link is
        # only stripped of its tracking parameter.
        cleaned = _strip_tracking(url, YOUTUBE_JUNK, GLOBAL_PREFIXES)
        return cleaned if cleaned != url else None
    if segments[:1] == ["watch"] and values.get("v"):
        return _build("https", "www.youtube.com", "/watch",
                      [("v", values["v"])] + keep, parts.fragment)
    if segments[:1] == ["shorts"] and len(segments) >= 2:
        return _build("https", "www.youtube.com", "/shorts/%s" % segments[1], [],
                      parts.fragment)
    return None  # playlists, channels, ... - not something we rewrite


def _spotify(url):
    """open.spotify.com -> no locale segment, no share tag.

    ``/intl-de/track/<id>?si=...`` becomes ``/track/<id>``: the locale segment
    only picks UI language and ``si`` is the share tag.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [segment for segment in (parts.path or "").split("/") if segment]
    if segments and segments[0].lower().startswith("intl-"):
        segments = segments[1:]
    path = "/" + "/".join(segments) if segments else (parts.path or "/")
    return _strip_tracking(
        _build("https", host, path, parse_qsl(parts.query, keep_blank_values=True),
               parts.fragment),
        _drop_all(SPOTIFY_JUNK), GLOBAL_PREFIXES)


def _steam(url):
    """store.steampowered.com / steamcommunity.com without the snr share tag."""
    return _strip_tracking(url, _drop_all(STEAM_JUNK), GLOBAL_PREFIXES)


def _nicovideo(url):
    """nicovideo.jp watch links without the ref/thumb_watch tracking."""
    return _strip_tracking(url, _drop_all(NICO_JUNK), GLOBAL_PREFIXES)


def _pixiv(url):
    """pixiv.net artwork -> the phixiv.net mirror, which Discord can embed.

    Only the artwork route is mirrored: phixiv redirects users, novels and
    tags back to pixiv, so rewriting those would only add a hop.  The
    language segment is kept (it picks the tag translation); the query is
    dropped - the artwork is the content, and pixiv's ``?p=`` page select
    has no place in the mirror's own form.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in ("pixiv.net", "www.pixiv.net"):
        return None  # sketch./dic. and friends are not artwork pages
    segments = [segment for segment in (parts.path or "").split("/") if segment]
    lang = ""
    if len(segments) == 3 and segments[0].lower() in PIXIV_LANGS:
        lang, segments = segments[0].lower() + "/", segments[1:]
    if (len(segments) == 2 and segments[0].lower() == "artworks"
            and segments[1].isascii() and segments[1].isdigit()):
        return _build("https", "www.phixiv.net",
                      "/%sartworks/%s" % (lang, segments[1]), [])
    return None


def _twitter(url):
    """A twitter/X status link -> the fxtwitter.com mirror, no query.

    The mirror serves the embed Discord needs; x.com itself often does not.
    Username-less ``/i/status/<id>`` links are resolved to
    ``/<user>/status/<id>`` before this runs (see `_twitter_resolve`), because
    the mirror only serves the ``/<user>/status/<id>`` form.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    user, status_id, tail = _twitter_parts(url)
    if not user or not status_id:
        return None  # /i/status/ without a resolved name - not ours to guess
    if host in TWITTER_SOURCE:
        host = TWITTER_MIRROR
    path = "/%s/status/%s" % (user, status_id)
    if tail:
        path += "/" + "/".join(tail)
    return _build("https", host, path, [])


def _zhihu(url):
    """zhihu.com / zhuanlan.zhihu.com -> the canonical shape, junk only.

    ``/question/<q>/answer/<a>`` and ``/answer/<a>`` keep their form (the short
    form is what the preview fetches and what people copy), ``/pin/<a>`` keeps
    the pin, and ``/p/<a>`` stays on the zhuanlan host.  Only the tracking
    parameters go; there is no known zhihu junk beyond the global families, and
    the answer in a URL is content.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [segment for segment in (parts.path or "").split("/") if segment]
    if len(segments) >= 4 and segments[0] == "question" and segments[2] == "answer":
        path = "/question/%s/answer/%s" % (segments[1], segments[3])
    elif len(segments) >= 2 and segments[0] == "answer":
        path = "/answer/%s" % segments[1]
    elif len(segments) >= 2 and segments[0] == "pin":
        path = "/pin/%s" % segments[1]
    elif len(segments) >= 2 and segments[0] == "p":
        path = "/p/%s" % segments[1]
    else:
        return None  # questions, people, columns: nothing to canonicalize
    if host in ("zhihu.com", "www.zhihu.com") and path.startswith("/p/"):
        host = "zhuanlan.zhihu.com"
    return _strip_tracking(_build("https", host, path,
                                  parse_qsl(parts.query, keep_blank_values=True),
                                  parts.fragment),
                           frozenset(), GLOBAL_PREFIXES)


def _bilibili(url):
    """bilibili.com -> canonical, minus the junk, keeping content.

    Dynamics get the permanent ``/opus/<id>`` form (``t.bilibili.com/<id>``
    and the old ``/dynamic/<id>`` are the same id space and the site never
    redirects there itself), columns get ``www.bilibili.com/read/cv<id>``, and
    videos keep their comment parameters and fragment.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [segment for segment in (parts.path or "").split("/") if segment]
    kind, ids = _bili_parts(url)
    if kind == "dynamic":
        return _build("https", "www.bilibili.com", "/opus/" + ids[0], [])
    if kind == "article":
        return _build("https", "www.bilibili.com", "/read/cv" + ids[0], [], parts.fragment)
    path = parts.path or "/"
    if len(segments) >= 2 and segments[0] == "video" and _BV_RE.match(segments[1]):
        extra = segments[2:]
        if extra and not (len(extra) == 1 and extra[0].lower() == "index.html"):
            return None  # an old-style path we do not understand - leave it be
        host, path = "www.bilibili.com", "/video/%s/" % segments[1]
    elif host in ("bilibili.com", "m.bilibili.com"):
        host = "www.bilibili.com"
    kept = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not (key == "p" and value == "1")]
    return _strip_tracking(_build("https", host, path, kept, parts.fragment),
                           _drop_all(BILI_JUNK), GLOBAL_PREFIXES)


# Canonical domains: a pure function url -> canonical url (or None to leave the
# link alone).  Adding one: (name, (hosts...), function).
SITES = (
    ("zhihu", ZHIHU_HOSTS, _zhihu),
    ("bilibili", ("bilibili.com", "t.bilibili.com"), _bilibili),
    ("youtube", ("youtube.com", "youtu.be"), _youtube),
    ("spotify", ("open.spotify.com", "spotify.com", "www.spotify.com"), _spotify),
    ("steam", ("steampowered.com", "steamcommunity.com"), _steam),
    ("nicovideo", ("nicovideo.jp",), _nicovideo),
    ("twitter", TWITTER_HOSTS, _twitter),
    ("pixiv", ("pixiv.net",), _pixiv),
)


def _site_for(host):
    rule = _rule(SITES, host)
    return rule[2] if rule else None


# ---------------------------------------------------------------------------
# twitter: /i/status/<id> -> /<user>/status/<id>
# ---------------------------------------------------------------------------

def _twitter_parts(url):
    """(user, status_id, tail) for a status link; user is None for /i/ forms."""
    parts = urlsplit(url)
    segments = [segment for segment in (parts.path or "").split("/") if segment]
    for index in (1, 2):
        if len(segments) > index + 1 and segments[index] in ("status", "statuses"):
            if not segments[index + 1].isdigit():
                return None, None, None
            user = segments[0]
            if user == "i":
                user = None  # /i/status/<id> and /i/web/status/<id> both
            return user, segments[index + 1], segments[index + 2:]
    return None, None, None


def _twitter_resolve(url, deadline):
    """Fill in the screen name of a username-less status link; None if unknown.

    Sources, in order: the official publish.twitter.com oEmbed (it answers
    with the canonical ``https://twitter.com/<user>/status/<id>``) and the
    FixTweet API.  Resolved names are cached by status id.
    """
    host = _host_of(url)
    if not _host_match(host, TWITTER_HOSTS):
        return None
    user, status_id, tail = _twitter_parts(url)
    if user or not status_id:
        return None
    entry = _cache_read(TWITTER_KEY, status_id)
    if entry:
        timestamp, name = entry
        if name and time.time() - timestamp < CACHE_TTL:
            return _twitter_join(url, name, status_id, tail)
        if not name and time.time() - timestamp < MISS_TTL:
            return None
    remaining = deadline - time.monotonic()
    if remaining <= 0.5:
        return None
    quoted = quote_plus("https://twitter.com/i/status/" + status_id)
    body = _get_body("https://publish.twitter.com/oembed?url=" + quoted,
                     min(2.0, remaining), limit=100_000)
    name = None
    if body:
        match = _TWEET_URL_RE.search(body.decode("utf-8", "replace"))
        if match:
            name = match.group("user")
    if not name:
        remaining = deadline - time.monotonic()
        if remaining > 0.5:
            body = _get_body("https://api.fxtwitter.com/status/" + status_id,
                             min(1.5, remaining), limit=200_000)
            if body:
                match = _SCREEN_NAME_RE.search(body.decode("utf-8", "replace"))
                if match:
                    name = match.group(1)
    _cache_write(TWITTER_KEY, status_id, name or "", CACHE_TTL, CACHE_MAX)
    if not name:
        log("no screen name for tweet %s" % status_id)
        return None
    return _twitter_join(url, name, status_id, tail)


def _twitter_join(url, user, status_id, tail):
    """Rebuild `url` with the resolved screen name in place of /i/."""
    parts = urlsplit(url)
    path = "/%s/status/%s" % (user, status_id)
    if tail:
        path += "/" + "/".join(tail)
    return urlunsplit(("https", parts.netloc, path, parts.query, ""))


# ---------------------------------------------------------------------------
# the Spotify -> YouTube bridge
# ---------------------------------------------------------------------------

def _fold(text):
    """lowercase, diacritics stripped - so Bjork matches Björk."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(plain.casefold().split())


def _spotify_track_id(url):
    """Track id for an open.spotify.com/track/<id> link, else None."""
    parts = urlsplit(url)
    if not _host_match((parts.hostname or "").lower(),
                       ("open.spotify.com", "spotify.com", "www.spotify.com")):
        return None
    segments = [segment for segment in (parts.path or "").split("/") if segment]
    if segments and segments[0].lower().startswith("intl-"):
        segments = segments[1:]
    if len(segments) == 2 and segments[0].lower() == "track" and _SPOTIFY_ID_RE.match(segments[1]):
        return segments[1]
    return None


def _spotify_meta(track_id, deadline):
    """(title, [artists], duration_ms) from the public embed page, or None."""
    remaining = deadline - time.monotonic()
    if remaining <= 0.5:
        return None
    body = _get_body(SPOTIFY_EMBED % track_id, min(2.0, remaining), limit=200_000)
    if not body:
        return None
    match = re.search(rb"__NEXT_DATA__[^>]*>(.*?)</script>", body, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    entity = None
    for node in _walk(data):
        if (isinstance(node, dict) and isinstance(node.get("name"), str)
                and isinstance(node.get("artists"), list)):
            entity = node
            break
    if entity is None:
        return None
    artists = [a.get("name") for a in entity["artists"]
               if isinstance(a, dict) and isinstance(a.get("name"), str)]
    title = entity["name"].strip()
    if not title:
        return None
    duration = entity.get("duration")
    return title, artists, duration if isinstance(duration, (int, float)) else None


def _yt_pairs(text):
    """(video_id, title, owner, seconds, artist_badge) rows from a payload.

    Owner, length and the verified-artist badge are pulled from the same
    renderer object the title comes from: they are what keep a fan cover
    from outranking the real video when the cover's title happens to carry
    the artist name and a matching length.  All are best-effort - a renderer
    without them still ranks, just without those points.
    """
    rows = []
    for match in _YT_PAIR_RE.finditer(text):
        video_id, raw_title = match.group(1), match.group(2)
        try:
            title = json.loads('"%s"' % raw_title)
        except ValueError:
            title = raw_title
        # The renderer object spans from the videoId to the next one (or the
        # end of the page): everything about this video sits inside.
        tail = text[match.end():text.find('"videoRenderer":', match.end())
                   if text.find('"videoRenderer":', match.end()) > 0
                   else len(text)]
        owner = _YT_OWNER_RE.search(tail)
        owner_name = ""
        if owner:
            try:
                owner_name = json.loads('"%s"' % owner.group(1))
            except ValueError:
                owner_name = owner.group(1)  # keep the raw text, as the title does
        seconds = _YT_LENGTH_RE.search(tail)
        rows.append((video_id, title,
                     owner_name,
                     seconds and _yt_seconds(seconds.group(1)) or None,
                     "BADGE_STYLE_TYPE_VERIFIED_ARTIST" in tail))
    return rows


def _yt_seconds(text):
    """"3:18" -> 198; None when the shape is unexpected."""
    parts = text.split(":")
    if not all(part.isdigit() for part in parts) or len(parts) > 3:
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


# Words that mark a Spotify name tail as a version/feature note rather than
# part of the title itself (" - Live", " (feat. X)", " - 2011 Remastered").
_VERSION_TAIL = ("live", "remaster", "mono", "stereo", "radio edit", "edit",
                 "version", "acoustic", "instrumental", "demo", "mix", "remix",
                 "deluxe", "bonus", "single", "feat", "with", "from", "take")

# Words in a YouTube title that mark a video as NOT the posted track: a fan
# cover, a karaoke take, a sped-up edit.  The hard list demotes such a video
# unless the track itself carries the same word (a "Acoustic" track matching
# an acoustic video is correct).  The soft list is the dance-music exception:
# "Original Mix" / "Extended Mix" / "[Instrumental]" are the *release name*
# on youtube for house and disco, so they only demote when the track name has
# no version word anywhere - "Mambos" matching "Mambos (Original Mix)" stays.
_COVER_HARD = re.compile(
    r"cover|covered by|カヴァー|カバー|歌ってみた|弾いてみた|踊ってみた"
    r"|8\s?bit|8-bit|ファミコン|アレンジ|arrangement|arr\.|drum cover"
    r"|karaoke|カラオケ|ニコカラ|reaction|リアクション|切り抜き"
    r"|sped up|slowed|reverb|nightcore|night core", re.IGNORECASE)
_COVER_SOFT = re.compile(r"\bmix\b|リミックス|remix|instrumental|inst\.?", re.IGNORECASE)
# A track name with any of these is itself a versioned release, so a video
# carrying a soft mark is plausibly the very same version.
_VERSION_WORDS = re.compile(
    r"\blive\b|remaster|mono|stereo|\bedit\b|version|acoustic|instrumental"
    r"|\bdemo\b|\bmix\b|remix|deluxe|bonus|\bsingle\b|\btake\b", re.IGNORECASE)


def _distinctive(text):
    """True when a needle is specific enough not to collide with other videos."""
    if len(text or "") >= BARE_TITLE_MIN:
        return True
    cjk = 0
    for char in text or "":
        if "\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff":
            cjk += 1
    return cjk >= BARE_TITLE_CJK


def _title_variants(track):
    """(full, base, tail) for a track name, all folded.

    Spotify names carry version notes that YouTube titles rarely repeat
    verbatim: ``Chega de Saudade - Live`` vs ``Chega de Saudade (Live)`` or
    ``... 1966 - CHEGA DE SAUDADE``.  Matching the full string misses those;
    matching only the base matches too much - so the base is the needle and
    the tail (when it looks like a version note) is a preference.  A
    parenthetical that is part of the title ("Time (Clock of the Heart)")
    keeps the full name as needle.
    """
    full = _fold(track)
    positions = [full.find(marker) for marker in (" - ", " (", " [", " – ")]
    positions = [position for position in positions if position > 0]
    if not positions:
        return full, full, ""
    cut = min(positions)
    base = full[:cut].strip(" -–")
    tail = full[cut:].strip(" -–()[]")
    if not base:
        return full, full, ""
    # Use the shortened base when the tail is a known version note ("- Live",
    # "(feat. X)") or when the base itself is specific enough - the latter is
    # what catches "- Slow" against a YouTube title of "(Slow)".
    if any(word in tail for word in _VERSION_TAIL) or _distinctive(base):
        return full, base, tail
    return full, full, tail


def _squash(text):
    """Fold punctuation and whitespace away: "Tune-Up" == "Tune Up"."""
    return " ".join(re.split(r"[^\w]+", text or ""))


def _contains(hay, needle):
    """Substring test, insensitive to punctuation; short needles need bounds.

    Spotify and YouTube disagree about hyphens, commas and spacing
    ("Tune-Up" vs "Tune Up (Slayd5000)", "Let's Go!Let's Go!ピース！ピース！"
    vs "Let's Go! Let's Go! ピース！ピース！"), so both sides are squashed to
    word sequences before the comparison.
    """
    if not needle:
        return False
    hay, needle = _squash(hay), _squash(needle)
    if not needle:
        return False
    if len(needle) <= 3 and needle.isascii():
        return re.search(r"\b%s\b" % re.escape(needle), hay) is not None
    return needle in hay


def _yt_rank(title, full, base, tail, artists, owner="", seconds=None,
             duration=None, artist_badge=False):
    """(title_hit, score) - `title_hit` gates acceptance, `score` orders hits.

    The tail is worth as much as the title itself: when Spotify says
    ``- Live``, the video that also says *live* is the one that was meant.
    A cover mark the track itself does not carry costs 4 points, more than
    a title hit is worth: the plain track was posted, so a fan cover, a
    karaoke take or a slowed edit of it is a wrong link even when its title
    mentions the artist and its length matches.  "Mix"/"instrumental" are
    softer: on dance music they are the *original* release name, so they
    only count against a video when the track name has no version word at
    all.

    The verified-artist badge, the channel name and the runtime are the
    signals that survive a language change: the official upload of a
    Japanese song often carries an English-only title, which a title match
    can never bridge, while the cover that does carry the Japanese title
    points at someone else's channel.  ``owner`` is the channel name,
    ``seconds`` the video length, ``duration`` the track's Spotify length.
    """
    hay = _fold(title)
    name = _fold(full)
    hit = _contains(hay, full) or _contains(hay, base)
    artist_here = any(_contains(hay, _fold(artist)) for artist in artists if artist)
    # An official upload with a translated title is a right hit the title
    # match cannot see - but only when the title still names the artist:
    # "Good luck to me tomorrow/ NANAOAKARI" for 明日の私に幸あれ.  The
    # badge alone must not open the gate, or a playlist, a talk show or
    # somebody else's song that merely features the artist walks in.
    # The badge is romanization-proof ("ナナヲアカリ OFFICIAL" never matches
    # the Spotify name "NANAOAKARI" by substring), so it leads.
    official = artist_badge or bool(owner) and (
        "official" in _fold(owner) or "公式" in (owner or ""))
    if not official and owner:
        official = any(_contains(owner, _fold(artist))
                       or _contains(_fold(artist), owner)
                       for artist in artists if artist)
    # The official hit needs the runtime too: the title can name the artist
    # on somebody else's song (a "feat." credit) or on a talk-show recap,
    # and only the length says this is the very recording.
    same_runtime = bool(seconds and duration
                        and abs(seconds - duration / 1000.0) <= 5)
    if (not hit and official and artist_here and same_runtime
            and not _COVER_HARD.search(title)):
        hit = True
    score = 2 if hit else 0
    if artist_here:
        score += 1
    if official:
        score += 2
    if tail and _contains(hay, tail):
        score += 2
    if same_runtime:
        score += 2  # the runtime survives every language and every retitle
    if _COVER_HARD.search(title) and not _COVER_HARD.search(full):
        score -= 4
    elif not _VERSION_WORDS.search(name) and _COVER_SOFT.search(title):
        score -= 4
    return hit, score


def _yt_search(query, deadline, limit=900_000):
    """(rows, corrected) - candidates from innertube, [] when it fails.

    ``corrected`` is YouTube's own "did you mean" for the query: a
    romanized artist next to a Japanese title makes search return nothing
    but a correction, and following it costs no extra request beyond the
    retry itself.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        return [], None
    payload = json.dumps({"context": {"client": YOUTUBE_CLIENT}, "query": query})
    body = _get_body(YOUTUBE_API, min(2.2, remaining), headers=YOUTUBE_COOKIE,
                     limit=limit, method="POST", body=payload.encode("utf-8"))
    if not body:
        return [], None
    text = body.decode("utf-8", "replace")
    corrected = None
    match = _YT_CORRECTED_RE.search(text)
    if match:
        runs = re.findall(r'"text":"((?:[^"\\]|\\.)*)"', match.group(1))
        try:
            corrected = "".join(json.loads('"%s"' % run) for run in runs).strip()
        except ValueError:
            corrected = None
    return _yt_pairs(text), corrected


def _yt_page(query, deadline):
    """Candidates from the public results page - innertube's safety net."""
    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        return []
    body = _get_body(YOUTUBE_SEARCH % quote_plus(query), min(2.0, remaining),
                     headers=YOUTUBE_COOKIE, limit=1_500_000)
    return _yt_pairs(body.decode("utf-8", "replace")) if body else []


def _yt_pick(rows, full, base, tail, artists, duration=None):
    """(found, near): the best accepted (video_id, title) and the best miss."""
    best, near, near_title = None, None, None
    for video_id, title, owner, seconds, artist_badge in rows[:10]:
        hit, score = _yt_rank(title, full, base, tail, artists, owner,
                              seconds, duration, artist_badge)
        if hit and (best is None or score > best[0]):
            best = (score, video_id, title)
        if near is None or score > near:
            near, near_title = score, title
    return ((best[1], best[2]) if best else None), (near, near_title)


# A shortened title is only used as a needle when it is specific enough that an
# unrelated video would not plausibly contain it: short generic names
# ("Precious", "Delight") stay on the full string, because a wrong link is
# worse than no link.  CJK is information-dense, so five kana/kanji count.
BARE_TITLE_MIN = 10
BARE_TITLE_CJK = 5


def _youtube_lookup(track, artists, deadline, duration=None):
    """(video_id, title) for a track, or None.

    Bounded attempts, each only when the previous one came back empty:
    the full "title + artists" query; YouTube's own "did you mean"
    correction (a romanized artist next to a Japanese title returns zero
    results but a suggestion, and the suggestion is the right query); the
    bare title when it is distinctive; the public results page when the
    API itself failed.  A candidate is accepted on a title hit, or on the
    artist's own channel carrying no cover mark - an official upload often
    has a translated title no needle can match.
    """
    full, base, tail = _title_variants(track)
    query = " ".join([track] + artists[:2])[:120]
    rows, corrected = _yt_search(query, deadline)
    found, near = _yt_pick(rows, full, base, tail, artists, duration)
    if found:
        return found
    tried = {query}
    if not rows and corrected and corrected not in tried and corrected != query:
        tried.add(corrected)
        rows, _ = _yt_search(corrected, deadline)
        found, near2 = _yt_pick(rows, full, base, tail, artists, duration)
        if near2[0] is not None and (near[0] is None or near2[0] > near[0]):
            near = near2
        if found:
            return found
    if not rows and len(base) < len(query) and base not in tried and _distinctive(base):
        tried.add(base)
        rows, _ = _yt_search(base, deadline)
        found, near2 = _yt_pick(rows, full, base, tail, artists, duration)
        if near2[0] is not None and (near[0] is None or near2[0] > near[0]):
            near = near2
        if found:
            return found
    if not rows:
        rows = _yt_page(query, deadline)
        found, near3 = _yt_pick(rows, full, base, tail, artists, duration)
        if near3[0] is not None and (near[0] is None or near3[0] > near[0]):
            near = near3
        if found:
            return found
    if near[1]:
        log("no title match for %r, best scored %d: %r" % (track, near[0], near[1]))
    return None


def _bridge_line(links, content, started, spoiled):
    """The 'YouTube: ...' line for the first Spotify track, or None."""
    track_id = None
    hidden = False
    for url in links:
        track_id = _spotify_track_id(url)
        if track_id:
            hidden = url in spoiled
            break
    if not track_id:
        return None
    for url in _candidates(content):
        if _host_match(_host_of(url), ("youtube.com", "youtu.be")):
            return None  # the message already carries a YouTube link
    entry = _cache_read(YOUTUBE_KEY, track_id)
    if entry:
        timestamp, video_id = entry
        if video_id and time.time() - timestamp < CACHE_TTL:
            return _youtube_line(video_id, hidden)
        if not video_id and time.time() - timestamp < MISS_TTL:
            return None
    deadline = started + min(BRIDGE_BUDGET, HANDLER_CAP)
    meta = _spotify_meta(track_id, deadline)
    if not meta:
        log("no spotify metadata for track %s" % track_id)
        return None
    track, artists, duration = meta
    found = _youtube_lookup(track, artists, deadline, duration)
    _cache_write(YOUTUBE_KEY, track_id, found[0] if found else "", CACHE_TTL, CACHE_MAX)
    if not found:
        log("no youtube match for %s (%s)" % (track_id, track))
        return None
    log("spotify %s (%s - %s) -> youtube %s (%s)" % (
        track_id, track, ", ".join(artists[:2]), found[0], found[1]))
    return _youtube_line(found[0], hidden)


# ---------------------------------------------------------------------------
# bilibili: dynamics and columns
# ---------------------------------------------------------------------------

_BILI_ID = r"\d{10,}"


def _bili_parts(url):
    """(kind, (id,)) for a dynamic or column link.

    ``dynamic`` covers ``/opus/<id>``, ``t.bilibili.com/<id>`` and the old
    ``/dynamic/<id>``; all three are the same id space.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [segment for segment in (parts.path or "").split("/") if segment]
    if host in BILI_DYNAMIC_HOSTS and segments and re.fullmatch(_BILI_ID, segments[0]):
        return "dynamic", (segments[0],)
    if _host_match(host, ("bilibili.com",)):
        if len(segments) >= 2 and segments[0] == "opus" and re.fullmatch(_BILI_ID, segments[1]):
            return "dynamic", (segments[1],)
        if len(segments) >= 2 and segments[0] == "dynamic" and re.fullmatch(_BILI_ID, segments[1]):
            return "dynamic", (segments[1],)
        if len(segments) >= 2 and segments[0] == "read" and \
                re.fullmatch(r"cv\d+", segments[1], re.IGNORECASE):
            return "article", (segments[1][2:],)
    return None, ()


def _bili_json(url, timeout, extra=None):
    """GET and decode a bilibili API JSON, or None.

    The dynamic API answers ``-352`` (risk control) to some requests even with
    a browser UA and a Referer, and the verdict is per request rather than per
    address: adding ``Origin`` - or using the app UA - gets ``code=0`` for the
    very same URL.  So a blocked call is retried on a different leg before the
    caller is told the post is unreadable; an HTML error page (the 412 wall)
    is not an answer either and likewise moves on to the next leg.
    """
    base = {"User-Agent": BILI_UA}
    base.update(BILI_HEADERS)
    base.update(extra or {})
    started = time.monotonic()
    payload = None
    legs = (BILI_UA, BILI_APP_UA,
            {"Origin": "https://www.bilibili.com"})
    for attempt, leg in enumerate(legs):
        # Each leg gets the time the whole budget still has, not a slice that
        # an earlier leg already spent.
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0.4:
            break
        headers = dict(base)
        if isinstance(leg, str):
            headers["User-Agent"] = leg
        else:
            headers.update(leg)
        body = _get_body(url, remaining, headers=headers, limit=300_000)
        if body:
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                continue  # an HTML error page (412 wall), not an answer: next leg
            if not isinstance(payload, dict):
                continue
            if payload.get("code") in (0, 4101152, 4101105, -404, 400004):
                return payload
        if timeout - (time.monotonic() - started) <= 0.4:
            break
    return payload


def _bili_dynamic(dynamic_id, timeout):
    """(author, text, [image urls], likes, comments) for a dynamic, or None.

    The API carries the text for most types, but a plain image post answers
    ``desc: null`` with no ``opus`` block - the caption is only in that post's
    rendered page, so when the API has none the ``/opus/`` page is read once.
    The page read gets only the time the API call has not already spent, so
    the two phases together stay inside `timeout`.
    """
    started = time.monotonic()
    payload = _bili_json(BILI_DYNAMIC_API % dynamic_id, timeout)
    if not payload or payload.get("code") != 0:
        log("bilibili dynamic %s: code=%s" % (dynamic_id, (payload or {}).get("code")))
        return None
    item = ((payload.get("data") or {}).get("item") or {})
    modules = item.get("modules") or {}
    author = ((modules.get("module_author") or {}).get("name") or "").strip()
    dynamic = modules.get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    text = _bili_text(dynamic)
    images = []
    for block in (major.get("draw") or {}).get("items") or []:
        if block.get("src"):
            images.append(block["src"])
    if not images:
        for block in (major.get("opus") or {}).get("pics") or []:
            if block.get("url"):
                images.append(block["url"])
    if not images:
        covers = (major.get("article") or {}).get("covers") or []
        if covers:
            images.append(covers[0])
    stat = modules.get("module_stat") or {}
    if not text:
        remaining = timeout - (time.monotonic() - started)
        if remaining > 0.5:
            text = _bili_clean_page_text(_bili_page_text(dynamic_id, remaining))
    return (author, _squash(text)[:300], images,
            (stat.get("like") or {}).get("count") or 0,
            (stat.get("comment") or {}).get("count") or 0)


def _bili_text(dynamic):
    """The post text, wherever this dynamic type keeps it.

    Measured positions: ``major.opus.summary.text`` for picture and text
    posts, ``major.article.desc`` for a long-form post, ``desc`` (a string, or
    a ``rich_text_nodes`` object for a repost) for a comment.  A type not seen
    in the archive is checked with the same probes rather than assumed.
    """
    major = dynamic.get("major") or {}
    for block in ("opus", "article", "word"):
        candidate = major.get(block) or {}
        for key in ("summary", "desc"):
            value = candidate.get(key) or {}
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict) and value.get("text"):
                return value["text"]
    raw = dynamic.get("desc")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return "".join(node.get("text") or node.get("orig_text") or ""
                        for node in (raw.get("rich_text_nodes") or []))
    return ""


def _bili_page_text(dynamic_id, timeout):
    """The caption from the /opus/ page, for posts the API leaves textless.

    A datacenter address is sometimes served a ~1 KB risk page instead of the
    rendered post, so a thin body is retried once with the ``spm_id_from``
    variant - that is the URL shape the app shares and it bypasses the
    interstitial.  Whatever comes back is parsed, never trusted.  `timeout`
    covers both attempts together, not each of them.
    """
    end = time.monotonic() + timeout
    body = None
    for suffix in ("", "?spm_id_from=333.999"):
        remaining = end - time.monotonic()
        if remaining <= 0.4:
            break
        body = _get_body("https://www.bilibili.com/opus/" + dynamic_id + suffix, remaining,
                         headers={"Accept": "text/html", "Accept-Language": "zh-CN,zh;q=0.9"},
                         limit=200_000)
        if body and len(body) > 4000:
            found = _bili_parse_page(body.decode("utf-8", "replace"))
            log("bilibili opus page %s%s: %dB -> %s" % (
                dynamic_id, suffix or "", len(body), "text" if found else "no text"))
            return found
    log("bilibili opus page %s: only a stub (%sB), no caption" % (
        dynamic_id, len(body or b"")))
    return ""


def _bili_parse_page(text):
    """The caption out of a rendered /opus/ page."""
    assigned = re.search(r"__INITIAL_STATE__\s*=\s*(\{.*?\})\s*[;<]", text, re.S)
    if assigned:
        for value in re.findall(r'"desc"\s*:\s*"((?:[^"\\]|\\.){4,600})"', assigned.group(1)):
            found = json.loads('"%s"' % value)
            if found.strip():
                return found
    # Fall back to the visible text between the timestamp and the counters.
    plain = re.sub(r"<script.*?</script>", " ", text, flags=re.S)
    plain = re.sub(r"<style.*?</style>", " ", plain, flags=re.S)
    plain = re.sub(r"<[^>]*>", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    found = re.search(r"\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*(.+?)\s*(?:\d+\s*硬币|1/4|展开更多|$)", plain)
    return found.group(1) if found and found.group(1).strip() else ""


# The share page ends the post with its own chrome: the image viewer's
# "收起 / 查看大图 / 向左旋转" controls, the coin prompt, the pager, the
# experience bar and live badges.  None of that is caption.
#
# It is peeled off by walking back from the end while the token is a known
# control, which is why prose survives: a token that is not chrome ends the
# walk immediately, so "我很喜欢这篇，分享给你们。点赞的都是好人" and even a
# caption that merely ends with a badge word ("这场直播的实况") are untouched.
# An earlier regex tried to recognise the whole trailing run at once and cut
# prose that merely contained a control word - this does not.
_BILI_WIDGETS = (
    "收起", "展开更多", "查看大图", "向左旋转", "向右旋转", "投上硬币", "经验值",
    "收藏", "分享", "点赞", "评论", "复制链接", "举报", "确定", "给UP主投上硬币",
)
_BILI_BADGES = ("实况", "正在直播", "已结束", "回放", "录播")
_BILI_PAGER = re.compile(r"^\d+\s*/\s*\d+(?:\s+\d+\s*/\s*\d+)*$")
_BILI_COIN = re.compile(r"^\d+\s*硬币$")
_BILI_XP = re.compile(r"^经验值(?:\+\d+)?(?:（今日[\d/]+）?)?$")


def _bili_is_chrome(token):
    """Is this one whitespace-separated token part of the share page's chrome?"""
    token = token.strip("()（）")
    return (token in _BILI_WIDGETS or token in _BILI_BADGES
            or bool(_BILI_PAGER.match(token)) or bool(_BILI_COIN.match(token))
            or bool(_BILI_XP.match(token)))

# A caption lifted from the page keeps its bare urls, and bilibili wraps them
# in bidi controls (U+200E LTRM, U+202A/202C embedding, U+2066/2069 isolates).
# Stripping the tags then leaves word-like fragments, so the controls go first
# and a bare url is removed with its caption, rather than shipping a link that
# lost its scheme.
_BIDI_CONTROLS = re.compile(
    "[\u200e\u200f\u202a-\u202e\u2066-\u2069\u200b\u200c\u200d\ufeff\u3164\u00a0\u3000]")
_BARE_URL = re.compile(r"(?:https?://)?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[^\s]*)?")
# A page that shreds a url into separate words ("… .com s n110 news de") cannot
# be repaired, and posting that is worse than posting nothing.
_BILI_LINKIFIED = re.compile(
    r"(?:[A-Za-z0-9_-]+[\s,]+){1,6}(?:com|cn|net|org|io|co|me|tv|jp|de|fr|uk|info|biz|app)\b")


def _bili_clean_page_text(text):
    """Normalise a caption lifted out of the rendered page."""
    text = _BIDI_CONTROLS.sub(" ", text or "")
    # A whole url goes first; only what is left can still be shredded prose.
    text = _BARE_URL.sub(" ", text)
    if _BILI_LINKIFIED.search(text):
        log("bilibili page caption is linkified; dropped rather than mangled")
        return ""
    tokens = text.split()
    cut = len(tokens)
    while cut > 0 and _bili_is_chrome(tokens[cut - 1]):
        cut -= 1
    if cut < len(tokens):
        text = " ".join(tokens[:cut])
    return text.strip(" ，。,.-")


def _bili_article(cv_id, timeout):
    """(author, title, [image urls], views, likes) for a column, or None."""
    payload = _bili_json(BILI_ARTICLE_API % cv_id, timeout)
    if not payload or payload.get("code") != 0:
        log("bilibili article cv%s: code=%s" % (cv_id, (payload or {}).get("code")))
        return None
    data = payload.get("data") or {}
    images = []
    for candidate in (data.get("origin_image_urls") or []) + (data.get("image_urls") or []):
        if candidate and candidate not in images:
            images.append(candidate)
    stats = data.get("stats") or {}
    return ((data.get("author_name") or "").strip(), (data.get("title") or "").strip(),
            images, stats.get("view") or 0, data.get("like") or 0)


def _json_safe(value):
    """A tuple becomes a list, so it survives the JSON cache unchanged."""
    return list(value) if isinstance(value, tuple) else value


BILI_MAX_CARD_IMAGE = 1    # one thumbnail; a card has room for a picture, not a gallery


def _bili_card(links, started, spoiled):
    """(card, spare) - one card for the first dynamic, the rest left as links.

    The platform folds a project's queued actions into a single message and
    only the first embed survives it, so exactly one dynamic is carded: the
    one that carries the picture, which is the reason to use a card at all.
    The other links are already in the text line (canonicalised by `_fix`), so
    there is nothing to add for them and nothing is lost.

    ``spare`` lists the dynamics that were not looked up at all - over the
    cap or out of time - so the handler can name them instead of dropping
    them.
    """
    for url in links:
        target = url
        rule = _rule(SHORTENERS, _host_of(url))
        if rule is not None and rule[0] == "bilibili" and not _already_canonical(url):
            # A b23.tv code can point at a dynamic as well as at a video, and
            # only the expansion tells them apart.  The link pass has just
            # resolved it (KV-cached), so this is normally a free lookup;
            # without it, a dynamic shared as a short code only ever got its
            # cleaned link, never a card.
            target = _expand_cached(
                url, min(started + HANDLER_CAP - 0.6,
                         time.monotonic() + REQUEST_TIMEOUT)) or url
        kind, ids = _bili_parts(target)
        if kind not in ("dynamic", "article"):
            continue
        cache_key = "%s:%s" % (kind, ids[-1])
        entry = _cache_read(BILI_KEY, cache_key)
        result = None
        if entry:
            age = time.time() - entry[0]
            value = entry[1]
            if isinstance(value, str) and not value and age < MISS_TTL:
                continue  # a recent fetch failure (deleted post)
            if isinstance(value, list) and len(value) == 5 and age < BILI_TTL:
                result = value
        if result is None:
            # Only a *fetch* needs budget: a cache hit costs nothing, so the
            # check comes after the cache is consulted.  With it before, a long
            # message skipped posts it already knew about.
            if started + HANDLER_CAP - time.monotonic() <= 0.6:
                return None, [url]  # no time left to look it up
            deadline = min(started + BILI_BUDGET, started + HANDLER_CAP)
            if deadline - time.monotonic() <= 0.5:
                return None, [url]
            got = _bili_article(ids[-1], min(1.5, deadline - time.monotonic())) \
                if kind == "article" else \
                _bili_dynamic(ids[-1], deadline - time.monotonic())
            if not got or len(got) != 5:
                _cache_write(BILI_KEY, cache_key, "", BILI_TTL, CACHE_MAX)
                continue
            _cache_write(BILI_KEY, cache_key, _json_safe(got), BILI_TTL, CACHE_MAX)
            result = _json_safe(got)
        author, text, images, likes, comments = result
        canonical = ("https://www.bilibili.com/opus/%s" % ids[-1]
                     if kind == "dynamic" else "https://www.bilibili.com/read/cv%s" % ids[-1])
        shown = "||%s||" % canonical if canonical in spoiled or url in spoiled else canonical
        counter = ("赞 %s · 评论 %s" % (likes, comments) if kind == "dynamic"
                   else "阅读 %s · 点赞 %s" % (likes, comments))
        fields = [{"name": "原文", "value": shown, "inline": False},
                  {"name": "互动", "value": counter, "inline": True}]
        if len(images) > 1:
            fields.append({"name": "图片", "value": "%d 张（见原动态）" % len(images),
                           "inline": True})
        # `title` and `description` must never be None: the first platform build
        # raised on a None description, and an empty title makes an unclickable
        # card.  The picture is the resize variant Discord can fetch directly.
        card = (shown, (author or ("动态 %s" % ids[-1]))[:256], (text or "")[:400] or None,
                _bili_thumb_url(images[0]) if images else None, fields)
        return card, []
    return None, []


def _bili_thumb_url(url):
    """A picture URL Discord can fetch as a card thumbnail."""
    if not url:
        return None
    if "@" in url or "hdslb.com/bfs/archive" in url:
        return url.replace("http://", "https://")
    return url.replace("http://", "https://") + "@672w"


# ---------------------------------------------------------------------------
# zhihu: previews without fxzhihu
# ---------------------------------------------------------------------------

_ZH_ID = r"\d{5,}"


def _zhihu_parts(url):
    """(kind, ids) for a zhihu URL: ('answer', (qid, aid)) / ('pin'|'article', (id,)).

    ``qid`` is ``""`` for the short ``/answer/<id>`` form - the preview does not
    need it, because the rendered page carries the question title.
    """
    parts = urlsplit(url)
    if not _host_match((parts.hostname or "").lower(), ZHIHU_HOSTS):
        return None, ()
    segments = [segment for segment in (parts.path or "").split("/") if segment]
    if len(segments) >= 3 and segments[0] == "question" and segments[2] == "answer":
        if re.fullmatch(_ZH_ID, segments[1]) and re.fullmatch(_ZH_ID, segments[3]):
            return "answer", (segments[1], segments[3])
    if len(segments) >= 2 and segments[0] == "answer" and re.fullmatch(_ZH_ID, segments[1]):
        return "answer", ("", segments[1])
    if len(segments) >= 2 and segments[0] == "pin" and re.fullmatch(_ZH_ID, segments[1]):
        return "pin", (segments[1],)
    if len(segments) >= 2 and segments[0] == "p" and re.fullmatch(_ZH_ID, segments[1]):
        return "article", (segments[1],)
    return None, ()


def _meta(text, name):
    """Content of a <meta> tag identified by property/itemprop/name.

    Zhihu's React renderer emits ``<meta data-rh="true" property="og:title"
    content="..."/>``: the name is a *value*, the attributes are in a varying
    order and carry extras, so the tag is parsed rather than pattern-matched.
    """
    for tag in re.findall(r"<meta\b[^>]*>", text):
        attributes = dict(re.findall(r'([\w:.-]+)="([^"]*)"', tag))
        if name in (attributes.get("property"), attributes.get("itemprop"),
                    attributes.get("name")) and "content" in attributes:
            return attributes["content"]
    return None


def _zhihu_preview(link, deadline):
    """(title, description) for an answer or a pin, or None.

    Answers come from the app-client page (no cookie, no signature); pins from
    the keyless v4 API, which needs no user-agent trick at all.  Both are
    cached in the KV store, so a link that has been seen once costs nothing.
    """
    kind, ids = _zhihu_parts(link)
    if kind not in ("answer", "pin"):
        return None
    cache_key = "%s:%s" % (kind, ids[-1])
    entry = _cache_read(ZHIHU_KEY, cache_key)
    if entry:
        age = time.time() - entry[0]
        value = entry[1]
        if isinstance(value, str) and not value and age < MISS_TTL:
            return None  # a recent miss; do not hammer the upstream
        if isinstance(value, list) and len(value) == 2 and age < ZHIHU_TTL:
            return value[0], value[1]
    remaining = deadline - time.monotonic()
    if remaining <= 0.5:
        return None
    if kind == "pin":
        result = _zhihu_pin(ids[0], min(2.0, remaining))
    else:
        result = _zhihu_answer(ids[1], min(2.5, remaining))
    _cache_write(ZHIHU_KEY, cache_key, list(result) if result else "", ZHIHU_TTL, CACHE_MAX)
    return result


def _zhihu_answer(answer_id, timeout):
    """Question title, author and excerpt from the app-client page."""
    body = _get_body("https://www.zhihu.com/answer/" + answer_id, timeout,
                     headers={"User-Agent": ZHIHU_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                     limit=200_000)
    if not body:
        log("zhihu answer %s: no page" % answer_id)
        return None
    text = body.decode("utf-8", "replace")
    title = _meta(text, "og:title")
    excerpt = _meta(text, "og:description") or _meta(text, "description")
    if not title:
        log("zhihu answer %s: no og:title in %d bytes" % (answer_id, len(body)))
        return None
    matched = re.match(r"^(.*) - (\S+) 的回答$", title)
    question, author = (matched.group(1).strip(), matched.group(2)) if matched else (title, "")
    if author:
        title = "%s · %s" % (question, author)
    excerpt = _squash(excerpt or "")
    return title, (excerpt[:300] if excerpt else "")


def _zhihu_pin(pin_id, timeout):
    """Author and excerpt from the keyless pins API."""
    body = _get_body(ZHIHU_PINS_API % pin_id, timeout, headers={"Accept": "application/json"},
                     limit=200_000)
    if not body:
        log("zhihu pin %s: no response" % pin_id)
        return None
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    author = ((data.get("author") or {}).get("name") or "").strip()
    excerpt = _squash(data.get("excerpt_title") or "")
    if not author and not excerpt:
        return None
    title = "%s 的想法" % author if author else "知乎想法"
    return title, (excerpt[:300] if excerpt else "")


def _zhihu_cards(links, content, started, spoiled):
    """(link, title, description) for at most one zhihu preview in this message.

    The original link is carried in the card, so a spoilered link stays
    spoilered there too.
    """
    for url in links:
        kind, _ids = _zhihu_parts(url)
        if kind in ("answer", "pin"):
            result = _zhihu_preview(url, started + ZHIHU_BUDGET)
            if result:
                # Show the canonical link, not the raw one, so the card never
                # hands out a tracking parameter again.
                canonical = _fix(url, started + ZHIHU_BUDGET) or url
                shown = "||%s||" % canonical if canonical in spoiled or url in spoiled else canonical
                return shown, result[0], result[1]
    return None, None, None


def _youtube_line(video_id, hidden=False):
    link = "https://www.youtube.com/watch?v=%s" % video_id
    return "YouTube: ||%s||" % link if hidden else "YouTube: %s" % link


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _cache_read(bucket, key):
    """(timestamp, value) for a well-formed entry, else None."""
    cache = kv_get(bucket)
    entry = cache.get(key) if isinstance(cache, dict) else None
    if not (isinstance(entry, list) and len(entry) == 2
            and isinstance(entry[0], (int, float))):
        return None
    value = entry[1]
    if not isinstance(value, (str, list)):
        return None
    if isinstance(value, list) and not all(
            isinstance(item, (str, int, float))
            or (isinstance(item, list) and all(isinstance(inner, str) for inner in item))
            for item in value):
        return None
    return entry[0], value


def _cache_write(bucket, key, value, ttl, cap):
    now = time.time()
    cache = kv_get(bucket)
    if not isinstance(cache, dict):
        cache = {}
    cache[key] = [now, value]
    # A value is a string (a hop target, a screen name, "") or a list of JSON
    # scalars, where one element may itself be a list of strings - a preview's
    # image urls.  The check is per element, not on the value as a whole.
    def _scalar(item):
        return isinstance(item, (str, int, float))

    def _storable(value_item):
        if _scalar(value_item):
            return True
        return (isinstance(value_item, list)
                and all(_scalar(inner) or (isinstance(inner, list)
                                            and all(_scalar(deep) for deep in inner))
                         for inner in value_item))

    fresh = dict((k, v) for k, v in cache.items()
                 if isinstance(v, list) and len(v) == 2
                 and isinstance(v[0], (int, float))
                 and isinstance(v[1], (str, list)) and _storable(v[1])
                 and now - v[0] < ttl)
    if len(fresh) > cap:
        newest = sorted(fresh.items(), key=lambda item: item[1][0], reverse=True)
        fresh = dict(newest[:cap])
    kv_set(bucket, fresh)


def _expand_cached(url, deadline):
    """One shortener hop, KV-cached.  Returns the target URL or None."""
    entry = _cache_read(CACHE_KEY, url)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1]
    remaining = deadline - time.monotonic()
    if remaining <= 0.2:
        log("out of budget before resolving %s" % url)
        return None
    try:
        target = _location(url, min(REQUEST_TIMEOUT, remaining))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        log("resolve failed for %s: %s: %s" % (url, type(exc).__name__, exc))
        return None
    if not target:
        log("no redirect from %s" % url)
        return None
    _cache_write(CACHE_KEY, url, target, CACHE_TTL, CACHE_MAX)
    return target


# ---------------------------------------------------------------------------
# the link pass
# ---------------------------------------------------------------------------

# `b23.tv/BV1…` and `b23.tv/av…` are plain permalinks, not redirects: expanding
# one costs a request and only produces the same video in a longer form.  The
# archive holds 875 of those against 35 real short codes, so the check is worth
# its line.
_PERMALINK_PATH = re.compile(r"/(?:BV[0-9A-Za-z]{8,}|av[0-9]+)/?$", re.IGNORECASE)


def _already_canonical(url):
    """True for a link that needs no work at all (b23.tv with a video id)."""
    parts = urlsplit(url)
    if not _host_match((parts.hostname or "").lower(), ("b23.tv",)):
        return False
    return bool(_PERMALINK_PATH.search(parts.path or ""))


def _fix(url, deadline):
    """Canonical form of `url`, or None when it is already as good as it gets."""
    original = url
    if _already_canonical(url):
        return None
    resolved = _twitter_resolve(url, deadline)
    if resolved:
        url = resolved
    expanded = False
    for _hop in range(MAX_HOPS):
        if _rule(SHORTENERS, _host_of(url)) is None:
            break
        target = _expand_cached(url, deadline)
        if not target:
            return None
        url = _tidy(target)
        if not _is_web_url(url):
            log("refusing non-web redirect target %r" % url)
            return None
        expanded = True
    if _rule(SHORTENERS, _host_of(url)) is not None:
        log("shortener chain did not end within %d hops: %s" % (MAX_HOPS, url))
        return None
    host = _host_of(url)
    if not host:
        return None
    cleaned = url
    site = _site_for(host)
    if site is not None:
        candidate = site(url)
        if candidate:
            cleaned = candidate
        elif _host_match(host, TWITTER_HOSTS):
            return None  # an unresolved /i/ form: leave it alone, do not guess
    if cleaned == url:
        cleaned = _strip_tracking(url, GLOBAL_TRACKING, GLOBAL_PREFIXES)
    if cleaned != url and not _cosmetic(cleaned, url):
        return cleaned
    if url != original:
        return url  # filling in the screen name was already the fix
    return url if expanded else None  # an expanded short link is already a win


def _cosmetic(left, right):
    """True when two URLs differ only in decorations: trailing slash, www.

    bilibili canonicalises to ``/video/<id>/`` and a bare ``/video/<id>``
    already works, so the one-character difference is not worth a message -
    whether or not the URL carries a query (``/video/avX?comment_on=1`` and
    ``/video/avX/?comment_on=1`` are the same page).  A bare ``www.`` on the
    host is the same kind of decoration (``youtube.com/shorts/<id>`` and
    ``www.youtube.com/shorts/<id>`` render identically in Discord), and the
    two may occur together: bilibili's canonical form adds both
    (``bilibili.com/video/avX`` -> ``www.bilibili.com/video/avX/``).  When a
    link carries real junk the rebuilt URL still gets reported, because then
    the query differs too.  Scheme and fragment are never decorations.
    """
    a, b = urlsplit(left), urlsplit(right)
    if a.scheme != b.scheme or a.query != b.query or a.fragment != b.fragment:
        return False
    if not a.path or a.path == "/" or not b.path or b.path == "/":
        return False  # dropping to a site root is a real change
    if a.path.rstrip("/") != b.path.rstrip("/"):
        return False
    ha, hb = a.netloc.lower(), b.netloc.lower()
    if ha == hb:
        return True
    # exact bare/dotted pair: example.com vs www.example.com
    bare, dotted = sorted((ha, hb), key=len)
    return bare != dotted and dotted == "www." + bare


def _candidates(text):
    """Distinct URLs in `text`, in order."""
    found = []
    for raw in _URL_RE.findall(text or ""):
        url = _tidy(raw)
        if url and len(url) <= 500 and url not in found:
            found.append(url)
    return found


def _spoiler_flags(text):
    """URLs that sit inside a Discord spoiler (||...||).

    The bars are counted before each match: an odd number means the link is
    inside an open spoiler, so the reply must hide the cleaned link too -
    otherwise the bot un-spoilers exactly what somebody chose to cover.
    """
    hidden = set()
    for match in _URL_RE.finditer(text or ""):
        if text[:match.start()].count("||") % 2:
            hidden.add(_tidy(match.group(0)))
    return hidden


def _handle(message, old_content):
    started = time.monotonic()
    content = message.get("content") or ""
    if not content:
        return
    links = _candidates(content)
    if old_content is not None:  # an edit: only links the edit introduced
        seen = set(_candidates(old_content))
        links = [url for url in links if url not in seen]
    if not links:
        return
    present = set(url.lower() for url in _candidates(content))
    spoiled = _spoiler_flags(content)
    deadline = started + TOTAL_BUDGET
    fixed, mentioned = [], set()
    for url in links:
        if len(fixed) >= MAX_LINKS or time.monotonic() > deadline:
            break
        if not _host_of(url):
            continue
        try:
            canonical = _fix(url, deadline)
        except Exception as exc:  # a handler must never die on user input
            log("giving up on %s: %s: %s" % (url, type(exc).__name__, exc))
            continue
        if not canonical or canonical in mentioned:
            continue
        if canonical.lower() in present:
            continue  # the message already shows this exact link
        mentioned.add(canonical)
        fixed.append((canonical, url in spoiled))
        log("cleaned %s -> %s" % (url, canonical))
    lines = [("||%s||" % link) if hidden else link for link, hidden in fixed]
    try:
        bridge = _bridge_line(links, content, started, spoiled)
    except Exception as exc:  # the bridge is a bonus, never a failure
        log("bridge failed: %s: %s" % (type(exc).__name__, exc))
        bridge = None
    if bridge:
        lines.append(bridge)
    # The text is sent after the cards, so a canonical link a card already
    # carries can be dropped instead of posted twice.  Only ONE embed survives
    # the platform's message fold, so when two cardable links turn up in one
    # message the second keeps its plain text line instead of disappearing
    # with the dropped embed.
    zhihu = None
    try:
        zhihu = _zhihu_cards(links, content, started, spoiled)
    except Exception as exc:  # a preview is a bonus, never a failure
        log("zhihu preview failed: %s: %s" % (type(exc).__name__, exc))
    zhihu_sent = bool(zhihu and zhihu[0])
    if zhihu_sent:
        carried = zhihu[0].replace("||", "").lower()
        lines = [line for line in lines if line.replace("||", "").lower() != carried]
        _link, title, description = zhihu
        fields = [{"name": "原链", "value": _link, "inline": False}]
        if description:
            fields.insert(0, {"name": "摘要", "value": description, "inline": False})
        send_embed(title=title[:250], description=None, color=ZHIHU_COLOR, fields=fields)
        log("zhihu preview for %s" % _link)
    bili_card, bili_spare = None, []
    try:
        bili_card, bili_spare = _bili_card(links, started, spoiled)
    except Exception as exc:  # a preview is a bonus, never a failure
        log("bilibili preview failed: %s: %s" % (type(exc).__name__, exc))
    bili_sent = False
    if bili_card and zhihu_sent:
        # The zhihu card already took this message's one embed: keep the
        # canonical line (sent below) so the dynamic stays visible as a link.
        log("bilibili card dropped (zhihu card has the embed): %s" % bili_card[0])
    elif bili_card:
        _link, title, description, thumb, fields = bili_card
        plain = (_link or "").replace("||", "").lower()
        lines = [line for line in lines if line.replace("||", "").lower() != plain]
        kwargs = {"title": title, "color": BILI_COLOR, "fields": fields}
        # Both are optional in the new signature, but a None *value* is what
        # broke the first build, so they are only passed when there is one.
        if description:
            kwargs["description"] = description
        if thumb:
            kwargs["thumbnail"] = thumb
        try:
            send_embed(**kwargs)
            bili_sent = True
        except Exception as exc:
            log("bilibili card failed: %s: %s" % (type(exc).__name__, exc))
    if bili_spare:
        # No time left to look these up: name them, never drop them.
        lines.append("另有 %d 条动态未展开：%s" % (
            len(bili_spare), " ".join("||%s||" % url if url in spoiled else url
                                     for url in bili_spare)))
    if bili_card or bili_spare:
        log("bilibili: card=%s, %d not looked up"
            % ("sent" if bili_sent else (bili_card and "dropped (zhihu)" or "none"),
               len(bili_spare)))
    if lines and any(line.strip() for line in lines):
        send("\n".join(lines))
    if lines or zhihu_sent or bili_sent:
        log("handled in %.2fs" % (time.monotonic() - started))


def on_message(message):
    _handle(message, None)


def on_message_edit(message):
    old = message.get("old_content")
    if old is None:
        # The original message was not cached: there is no way to tell which
        # links the edit introduced, and re-answering everything the message
        # carries would repost links the bot already cleaned.  Silent by
        # default applies here too.
        return
    _handle(message, old)


def on_failure(event_name, event_data, error):
    log("on_failure %s: %s: %s" % (event_name, type(error).__name__, error))
