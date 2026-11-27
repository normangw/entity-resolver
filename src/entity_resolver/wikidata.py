"""Live Wikidata lookup, used when a name is not in the local alias index.

The local index only knows a few companies. Wikidata also records private
subsidiaries (YouTube, Beats Electronics, ...), so on an index miss the service
asks Wikidata and reads the owner from the ``parent organization`` (P749) or
``owned by`` (P127) statements. ``lookup`` is blocking network I/O, so async
callers must run it off the event loop (``asyncio.to_thread``).

Answers, including "no such company", are cached in memory for a while: Wikidata
throttles quickly and a lookup costs three or more requests. Failures are never
cached, so a throttled name is retried on the next call.

Each lookup has a deadline: no API call, retry or wait starts after it. Timeouts
and 5xx are retried with backoff. A 429 is waited out once when its
``Retry-After`` is short and fits in the deadline; otherwise the lookup raises
``WikidataUnavailable`` carrying that ``Retry-After``. A lookup counts once toward
the circuit breaker, however many calls it made. See docs/time-limits.md.
"""

import datetime as dt
import email.utils
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from rapidfuzz import fuzz

from entity_resolver.observability import metrics
from entity_resolver.resolver import Entity, Match, normalize

logger = logging.getLogger(__name__)

_API = "https://www.wikidata.org/w/api.php"
# Wikimedia asks API clients to identify themselves.
_USER_AGENT = "entity-resolver/0.1 (DSAN 6700 class project)"

_PARENT_PROPS = ("P749", "P127")  # parent organization, owned by
_HQ_PROP = "P159"  # headquarters location
# A search hit is treated as a company only if it has one of these, which rules
# out planets, people and songs that share a name with a company.
_COMPANY_PROPS = (*_PARENT_PROPS, _HQ_PROP, "P452")  # P452 = industry
_END_TIME = "P582"
_SHARE = "P1107"  # proportion: the fraction of the entity an owner holds
_HUMAN = "Q5"
_INSTANCE_OF = "P31"
# Things Wikidata gives an owner or a location that are not companies (the Empire
# State Building has an owner and Georgetown has a headquarters). A hit whose
# "instance of" is one of these is skipped. Not exhaustive: it lists the classes
# testers hit, and a company tagged only with other classes still passes.
_NOT_COMPANY_CLASSES = frozenset(
    {
        "Q41176",
        "Q11303",
        "Q1021645",  # building, skyscraper, office building
        "Q3918",
        "Q38723",
        "Q902104",
        "Q875538",  # university, higher education,
        "Q62078547",
        "Q1371037",
        "Q15936437",
        "Q23002054",  # private/public ones
        "Q484652",
        "Q245065",  # international / intergovernmental organization
        "Q327333",
        "Q7278",
        "Q33506",  # government agency, political party, museum
        "Q163740",
        "Q79913",  # nonprofit organization, NGO
        "Q4120211",
        "Q1335818",  # regional organization, supranational union (EU)
        "Q1345691",
        "Q105758271",
        "Q15925165",  # financial institution, development bank, UN agency (World Bank)
        "Q97234439",  # city government ("City of Chicago")
        "Q1194093",
        "Q1968122",
        "Q336473",  # international NGO, national Red Cross society, aid agency
    }
)
# How many search hits to read. Few hits let other meanings of a common word
# ("Mars" the planet) crowd out the company.
_SEARCH_LIMIT = "25"
# "owned by" (P127) also records shareholders, so it only counts as a parent when
# the owner holds at least this fraction (statements with no stake stay in).
_MIN_OWNER_SHARE = 0.5
# A parent named like the entity itself ("WhatsApp LLC" for WhatsApp) is its
# legal-entity wrapper, not a real owner; hop through it, at most this far.
_MAX_WRAPPER_HOPS = 2

Fetch = Callable[[dict[str, str]], dict[str, Any]]


def _http_fetch(timeout: float, api_url: str = _API) -> Fetch:
    """A blocking caller of ``api_url`` that gives up after ``timeout`` seconds."""

    def fetch(params: dict[str, str]) -> dict[str, Any]:
        """GET the API with ``params`` and return the JSON object."""
        query = urllib.parse.urlencode({**params, "format": "json"})
        request = urllib.request.Request(
            f"{api_url}?{query}", headers={"User-Agent": _USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data: dict[str, Any] = json.load(response)
        if "error" in data:  # throttling can arrive as HTTP 200 with an error body
            raise ValueError(f"Wikidata API error: {data['error'].get('code')}")
        return data

    return fetch


# Seconds we tell our own callers to wait when Wikidata gave no usable Retry-After.
DEFAULT_RETRY_AFTER = 5.0


class WikidataUnavailable(Exception):
    """Wikidata could not be reached, throttled us, or sent an unusable reply.

    ``retry_after`` is how many seconds the caller should wait before asking again
    (Wikidata's own ``Retry-After`` when it sent one).
    """

    def __init__(self, message: str, retry_after: float = DEFAULT_RETRY_AFTER):
        """Keep ``message`` and the suggested wait ``retry_after``."""
        super().__init__(message)
        self.retry_after = retry_after


class _Throttled(Exception):
    """Wikidata answered 429 and we will not wait for it within this lookup."""

    def __init__(self, retry_after: float | None):
        """Keep Wikidata's ``retry_after`` (None when it sent none we can read)."""
        super().__init__("HTTP Error 429: Too Many Requests")
        self.retry_after = retry_after


class _DeadlineExceeded(TimeoutError):
    """The lookup used up ``deadline`` seconds; no further call is started."""


def _retryable(exc: Exception) -> bool:
    """True for a timeout, a dropped connection or a 5xx, which may pass.

    A 429 is handled on its own (see ``WikidataClient._fetch``); other 4xx and a
    bad JSON body are never retried, since they cannot succeed.
    """
    if isinstance(exc, _DeadlineExceeded):
        return False
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, OSError)


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """Seconds from a ``Retry-After`` header (seconds or an HTTP date), or None."""
    value = exc.headers.get("Retry-After") if exc.headers is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - dt.datetime.now(dt.UTC)).total_seconds())


class CircuitBreaker:
    """Stops calling a failing service for a while.

    After ``threshold`` failures in a row it opens for ``cooldown`` seconds, during
    which ``allow`` is False; then one call is let through, and a success closes it.
    """

    def __init__(
        self,
        threshold: int = 5,
        cooldown: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Open after ``threshold`` failures and stay open for ``cooldown`` seconds.

        ``clock`` is injectable so tests can move time without sleeping.
        """
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """True when a call may go out. A call past the cooldown is the probe."""
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at >= self._cooldown:
                # Half open: let this call probe, and keep others out until it ends.
                self._opened_at = self._clock()
                return True
            return False

    def success(self) -> None:
        """Close the breaker and clear the failure count."""
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def failure(self) -> None:
        """Count a failure and record when the circuit breaker opens."""
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                if self._opened_at is None:
                    metrics.inc("wikidata_breaker_opens_total")
                self._opened_at = self._clock()

    @property
    def is_open(self) -> bool:
        """True after the breaker opens, until a success closes it."""
        with self._lock:
            return self._opened_at is not None

    def retry_after(self) -> float:
        """Seconds until the breaker lets a probe through (0 when it is closed)."""
        with self._lock:
            if self._opened_at is None:
                return 0.0
            return max(0.0, self._cooldown - (self._clock() - self._opened_at))


def _share(statement: dict[str, Any]) -> float | None:
    """The stake a statement records (0-1), or None when it records none."""
    for snak in statement.get("qualifiers", {}).get(_SHARE, []):
        try:
            return float(snak["datavalue"]["value"]["amount"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _statement_ids(
    claims: dict[str, Any],
    prop: str,
    include_ended: bool = False,
    min_share: float = 0.0,
) -> list[str]:
    """Entity ids a property points to, skipping deprecated statements.

    Statements with an end time are skipped unless ``include_ended`` is set, and
    so are statements that record a stake below ``min_share``.
    """
    ids: list[str] = []
    for statement in claims.get(prop, []):
        snak = statement["mainsnak"]
        share = _share(statement)
        if (
            statement.get("rank") == "deprecated"
            or (not include_ended and _END_TIME in statement.get("qualifiers", {}))
            or snak.get("snaktype") != "value"
            or (share is not None and share < min_share)
        ):
            continue
        ids.append(snak["datavalue"]["value"]["id"])
    return ids


class WikidataClient:
    """Resolves a company name through the Wikidata search and entity APIs."""

    def __init__(
        self,
        timeout: float = 10,
        threshold: float = 0.8,
        fetch: Fetch | None = None,
        cache_ttl: float = 3600,
        cache_size: int = 1024,
        clock: Callable[[], float] = time.monotonic,
        retries: int = 0,
        backoff: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        breaker: CircuitBreaker | None = None,
        api_url: str = _API,
        deadline: float = 15.0,
        max_retry_after: float = 2.0,
    ) -> None:
        """``cache_ttl`` is in seconds; 0 turns the cache off.

        ``retries`` extra attempts (with exponential backoff and jitter) are made
        for timeouts and 5xx on each request. A 429 whose ``Retry-After`` is at most
        ``max_retry_after`` seconds is waited out once per lookup. No call starts
        after ``deadline`` seconds into a lookup. ``breaker`` short-circuits lookups
        while Wikidata keeps failing; ``api_url`` is the MediaWiki API to ask.
        """
        self._threshold = threshold
        self._raw_fetch = fetch or _http_fetch(timeout, api_url)
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep
        self._deadline = deadline
        self._max_retry_after = max_retry_after
        self.breaker = breaker or CircuitBreaker(threshold=10**9)
        self._ttl = cache_ttl
        self._max_entries = cache_size
        self._clock = clock
        self._cache: OrderedDict[tuple[str, str], tuple[float, Match | None]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()  # lookups run on several worker threads
        # Per lookup, per worker thread: when it must end, and whether its one
        # 429 wait has been used.
        self._state = threading.local()

    def lookup(self, name: str, language: str = "en") -> Match | None:
        """Return the best company match for ``name``, or None (blocking call).

        None means Wikidata answered and had no company by that name. If it
        could not answer (network failure, HTTP 429, bad JSON, deadline) this
        raises ``WikidataUnavailable`` so an outage is not mistaken for "not
        found". A failed lookup counts once toward the breaker, however many
        calls and retries it made.
        """
        key = (normalize(name), language)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and self._clock() - cached[0] < self._ttl:
                self._cache.move_to_end(key)
                metrics.inc("wikidata_cache_requests_total", result="hit")
                return cached[1]
        if self._ttl > 0:
            metrics.inc("wikidata_cache_requests_total", result="miss")
        if not self.breaker.allow():
            raise WikidataUnavailable(
                "circuit open: Wikidata keeps failing",
                retry_after=max(1.0, self.breaker.retry_after()),
            )
        self._state.ends_at = self._clock() + self._deadline
        self._state.waited_429 = False
        try:
            match = self._lookup(name, language)
        except _Throttled as exc:
            logger.warning("Wikidata lookup throttled (429)")
            self.breaker.failure()
            wait = DEFAULT_RETRY_AFTER if exc.retry_after is None else exc.retry_after
            raise WikidataUnavailable(str(exc), retry_after=max(1.0, wait)) from exc
        except (OSError, ValueError) as exc:  # network failure, deadline, bad JSON
            # External error text may contain a private company name or URL.
            logger.warning("Wikidata lookup failed")
            self.breaker.failure()
            raise WikidataUnavailable(str(exc)) from exc
        self.breaker.success()
        if self._ttl > 0:
            with self._lock:
                self._cache[key] = (self._clock(), match)
                self._cache.move_to_end(key)
                while len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)
        return match

    def _left(self) -> float:
        """Seconds left before this lookup's deadline (inf outside a lookup)."""
        ends_at = getattr(self._state, "ends_at", None)
        return float("inf") if ends_at is None else ends_at - self._clock()

    def _fetch(self, params: dict[str, str]) -> dict[str, Any]:
        """One API call, retried with backoff and jitter while the deadline allows.

        Timeouts, dropped connections and 5xx are retried up to ``retries`` times.
        A 429 is waited out once per lookup when its ``Retry-After`` is at most
        ``max_retry_after`` and fits before the deadline; otherwise it ends the
        lookup (``_Throttled``). No call or wait starts past the deadline. Each
        attempt's duration goes to the Wikidata latency histogram.
        """
        attempt = 0
        while True:
            if self._left() <= 0:
                raise _DeadlineExceeded("Wikidata lookup deadline passed")
            try:
                started = time.perf_counter()
                try:
                    return self._raw_fetch(params)
                finally:
                    metrics.observe_wikidata(time.perf_counter() - started)
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    error: Exception = exc
                else:
                    wait = _retry_after(exc)
                    if (
                        wait is None
                        or wait > self._max_retry_after
                        or getattr(self._state, "waited_429", False)
                        or wait * 1.25 >= self._left()
                    ):
                        raise _Throttled(wait) from exc
                    self._state.waited_429 = True
                    # Never earlier than Wikidata asked; jitter spreads the retries.
                    self._sleep(wait * (1 + 0.25 * random.random()))  # noqa: S311
                    continue
            except Exception as exc:
                error = exc
            delay = self._backoff * 2**attempt * (0.5 + random.random())  # noqa: S311
            if attempt >= self._retries or not _retryable(error):
                raise error
            if delay >= self._left():
                raise _DeadlineExceeded("Wikidata lookup deadline passed") from error
            attempt += 1
            self._sleep(delay)

    def _lookup(self, name: str, language: str) -> Match | None:
        """The best company Wikidata has for ``name``, or None.

        Search is always in English. ``language`` only chooses the labels that come
        back. Several companies sharing a name are broken by score, then by how many
        Wikipedia sitelinks the entity has.
        """
        # Always search in English so the same name finds the same company whatever
        # the display language is; ``language`` only chooses the labels we return.
        hits = self._fetch(
            {
                "action": "wbsearchentities",
                "search": name,
                "language": "en",
                "uselang": "en",
                "type": "item",
                "limit": _SEARCH_LIMIT,
            }
        )["search"]
        wanted = normalize(name)
        scored: list[tuple[dict[str, Any], float]] = []
        for hit in hits:
            matched = hit.get("match", {}).get("text") or hit.get("label", "")
            score = fuzz.WRatio(wanted, normalize(matched)) / 100
            if score >= self._threshold:
                scored.append((hit, score))
        if not scored:
            return None

        entities = self._fetch(
            {
                "action": "wbgetentities",
                "ids": "|".join(hit["id"] for hit, _ in scored),
                "props": "claims|sitelinks",
            }
        )["entities"]
        # Several companies can share a name ("Siemens AG" also finds Siemens
        # Limited in India). Take the best score, then the most widely documented
        # one (most Wikipedia sitelinks), which is almost always the one meant.
        companies: list[tuple[float, int, dict[str, Any], dict[str, Any]]] = []
        # Best score among hits skipped as non-companies. If the name matches a
        # university better than any company, the input names the university, and
        # a related company-like hit ("Georgetown University Press") is not it.
        best_non_company = 0.0
        for hit, score in scored:
            entity = entities[hit["id"]]
            claims = entity.get("claims", {})
            kinds = set(_statement_ids(claims, _INSTANCE_OF, include_ended=True))
            if kinds & _NOT_COMPANY_CLASSES:
                best_non_company = max(best_non_company, score)
                continue
            if any(prop in claims for prop in _COMPANY_PROPS):
                links = len(entity.get("sitelinks", {}))
                companies.append((score, links, hit, claims))
        if not companies:
            return None
        companies.sort(key=lambda c: (c[0], c[1]), reverse=True)
        score, _, hit, claims = companies[0]
        if best_non_company > score:
            return None
        ambiguous = any(c[0] == score for c in companies[1:])
        match = self._build(hit, claims, score, language)
        return Match(match.entity, match.score, ambiguous=ambiguous)

    def _build(
        self, hit: dict[str, Any], claims: dict[str, Any], score: float, language: str
    ) -> Match:
        """A match from one search hit, including its parent and headquarters.

        ``canonical_name`` stays English (the search runs with ``uselang=en``, so
        the hit's label is English). When ``language`` is not English, the label in
        that language is fetched and goes in ``label`` (#113).
        """
        canonical = hit.get("label", hit["id"])
        parent_ids = self._owner_ids(claims)
        hq_ids = _statement_ids(claims, _HQ_PROP)[:1]
        # One labels call covers the entity itself, its owners and its HQ.
        labels = self._labels([hit["id"], *parent_ids, *hq_ids], language)
        # ``_labels`` falls back to English, so an entity with no label in this
        # language comes back equal to ``canonical`` and gets no ``label``.
        translated = labels.pop(hit["id"], canonical)
        own_names = {normalize(canonical), normalize(translated)}
        # The first owner with a label wins: P749 before P127, and an owner
        # that has no label in this language (or English) is skipped.
        parent_id = next((i for i in parent_ids if i in labels), None)
        for _ in range(_MAX_WRAPPER_HOPS):
            if parent_id is None or normalize(labels[parent_id]) not in own_names:
                break
            parent_id = self._owner_of(parent_id, labels, language)
        if parent_id is not None and normalize(labels[parent_id]) in own_names:
            parent_id = None  # only ever found the entity's own wrapper
        entity = Entity(
            wikidata_id=hit["id"],
            canonical_name=canonical,
            parent_company=labels[parent_id] if parent_id else None,
            headquarters=labels.get(hq_ids[0]) if hq_ids else None,
            label=translated if translated != canonical else None,
        )
        return Match(entity=entity, score=round(score, 3))

    def _owner_ids(
        self, claims: dict[str, Any], include_ended: bool = False
    ) -> list[str]:
        """Candidate parents of an entity, most trusted first.

        ``parent organization`` (P749) is taken as is. ``owned by`` (P127) is
        only used when it names exactly one owner with a majority or unstated
        stake, and that owner is not a person: a company with several owners is
        a widely held one (Caterpillar's holders are Vanguard, BlackRock...), and
        a founder holding shares (Terry Gou in Foxconn) is not a parent company.
        """
        parents = _statement_ids(claims, "P749", include_ended)
        if parents:
            return parents
        owners = list(
            dict.fromkeys(
                _statement_ids(claims, "P127", include_ended, _MIN_OWNER_SHARE)
            )
        )
        if len(owners) != 1 or self._is_human(owners[0]):
            return []
        return owners

    def _is_human(self, qid: str) -> bool:
        """True when ``qid`` is an instance of human."""
        claims = self._fetch(
            {"action": "wbgetclaims", "entity": qid, "property": "P31"}
        )["claims"]
        return _HUMAN in _statement_ids(claims, "P31", include_ended=True)

    def _owner_of(self, qid: str, labels: dict[str, str], language: str) -> str | None:
        """The labelled owner of ``qid``, preferring current over ended statements.

        Adds the owners' labels to ``labels``. Ended statements are a last
        resort: a wrapper entity is often merged away while the brand keeps its
        owner (WhatsApp LLC's link to Meta has an end date).
        """
        claims = self._fetch(
            {"action": "wbgetentities", "ids": qid, "props": "claims"}
        )["entities"][qid].get("claims", {})
        for include_ended in (False, True):
            ids = self._owner_ids(claims, include_ended)
            labels.update(self._labels(ids, language))
            owner = next((i for i in ids if i in labels), None)
            if owner is not None:
                return owner
        return None

    def _labels(self, ids: list[str], language: str) -> dict[str, str]:
        """Labels for ``ids`` in ``language``, falling back to English."""
        if not ids:
            return {}
        entities = self._fetch(
            {
                "action": "wbgetentities",
                "ids": "|".join(dict.fromkeys(ids)),
                "props": "labels",
                "languages": "|".join(dict.fromkeys([language, "en"])),
            }
        )["entities"]
        out: dict[str, str] = {}
        for qid, entity in entities.items():
            labels = entity.get("labels", {})
            label = labels.get(language) or labels.get("en")
            if label:
                out[qid] = label["value"]
        return out
