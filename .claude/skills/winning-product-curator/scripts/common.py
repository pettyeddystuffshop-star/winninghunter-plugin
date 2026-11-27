"""Shared helpers for the winning-product-curator skill.

Standard library only, so every script runs with a stock Python 3.10+.
Covers: project paths, .env loading, rules config, logging, canonical JSON
and SHA-256 hashing, rate limiting, HTTP with retry/backoff, and the
read-only Shopify GraphQL guard used throughout Phase A.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = SKILL_DIR / "schemas"
CONFIG_PATH = SKILL_DIR / "config" / "rules.json"


def project_root() -> Path:
    """Repository root: WPC_PROJECT_ROOT, else the directory holding .claude/."""
    env = os.environ.get("WPC_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    # .claude/skills/winning-product-curator -> project root is 3 levels up
    return SKILL_DIR.parent.parent.parent


# --------------------------------------------------------------------------- env

def load_env(path: Path | None = None, override: bool = False) -> None:
    """Load KEY=VALUE pairs from .env into os.environ (no third-party deps)."""
    path = path or project_root() / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        if override or key not in os.environ:
            os.environ[key] = value


def require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)} (see .env.example)")
    return {n: os.environ[n] for n in names}


def load_rules() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------- logging

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(os.environ.get("WPC_LOG_LEVEL", "INFO").upper())
    return logger


# ------------------------------------------------------------------- time/dates

def store_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(os.environ.get("STORE_TIMEZONE", "UTC"))


def store_today() -> dt.date:
    return dt.datetime.now(store_tz()).date()


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value[:10])


def parse_ts(value: str) -> dt.datetime:
    v = value.replace("Z", "+00:00")
    ts = dt.datetime.fromisoformat(v)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts


# ---------------------------------------------------------------- JSON + hashing

def canonical_json(obj: Any) -> str:
    """Deterministic serialisation used for every SHA-256 in this skill."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path | str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path | str, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def validate_against_schema(instance: Any, schema_name: str) -> list[str]:
    """Validate with jsonschema when installed; returns a list of error strings.

    jsonschema is listed in requirements.txt. If it is absent, a hard error is
    returned rather than silently skipping validation.
    """
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return ["jsonschema is not installed: run `pip install -r .claude/skills/winning-product-curator/requirements.txt`"]
    schema = read_json(SCHEMA_DIR / schema_name)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        loc = "/" + "/".join(str(p) for p in err.absolute_path)
        errors.append(f"{loc}: {err.message}")
    return errors


# ------------------------------------------------------------------ rate limiting

class RateLimiter:
    """Sliding-window limiter: at most `max_calls` within `period` seconds."""

    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._calls: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.period - (now - self._calls[0]) + 0.01
            time.sleep(max(wait, 0.01))


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")
        self.status = status
        self.body = body
        self.url = url


class FatalApiError(RuntimeError):
    """Raised for errors where retrying cannot help (auth, plan gate, credits)."""


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    body: Any = None,
    limiter: RateLimiter | None = None,
    max_retries: int = 5,
    timeout: float = 60.0,
    fatal_check=None,
) -> Any:
    """JSON request with exponential backoff on 429/5xx/network errors.

    `fatal_check(status, parsed_body)` may raise FatalApiError to stop retries
    (e.g. WinningHunter credit exhaustion, which also returns 429).
    """
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(clean, doseq=True)}"
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "winning-product-curator/1.0"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    log = get_logger("http")
    delay = 2.0
    for attempt in range(max_retries + 1):
        if limiter:
            limiter.acquire()
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8") or "null"
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if fatal_check:
                fatal_check(exc.code, parsed)
            if exc.code in (401, 403):
                raise FatalApiError(f"HTTP {exc.code} from {url}: {raw[:300]}") from exc
            if exc.code == 429 or exc.code >= 500:
                if attempt == max_retries:
                    raise HttpError(exc.code, raw, url) from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                sleep_for = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else delay
                log.warning("HTTP %s on %s; retry %d/%d in %.1fs", exc.code, url.split("?")[0], attempt + 1, max_retries, sleep_for)
                time.sleep(min(sleep_for, 30.0))
                delay = min(delay * 2, 30.0)
                continue
            raise HttpError(exc.code, raw, url) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == max_retries:
                raise
            log.warning("Network error on %s (%s); retry in %.1fs", url.split("?")[0], exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise RuntimeError("unreachable")


# ------------------------------------------------------ Shopify read-only guard

_COMMENT_RE = re.compile(r"#[^\n]*")
_STRING_RE = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"')


class MutationBlockedError(PermissionError):
    pass


def assert_read_only_graphql(document: str) -> None:
    """Reject any GraphQL document that contains a mutation or subscription.

    String literals and comments are stripped first so a product title such as
    "mutation-proof case" cannot trigger a false positive, and an operation
    hidden after a comment cannot slip through.
    """
    stripped = _STRING_RE.sub('""', document)
    stripped = _COMMENT_RE.sub("", stripped)
    if re.search(r"\b(mutation|subscription)\b", stripped):
        raise MutationBlockedError(
            "Phase A is read-only: GraphQL mutations/subscriptions are blocked. "
            "Writes are only possible via apply_approved_plan.py with a verified, approved Change Plan."
        )
    if not re.search(r"^\s*(query\b|fragment\b|\{)", stripped):
        raise MutationBlockedError("Only GraphQL `query` operations (or anonymous `{...}` queries) are permitted in Phase A.")


class ShopifyGraphQL:
    """Admin GraphQL client. read_only=True (default) blocks every mutation."""

    def __init__(self, shop_domain: str, token: str, api_version: str, *, read_only: bool = True):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", shop_domain):
            raise SystemExit(f"SHOPIFY_SHOP_DOMAIN must look like your-store.myshopify.com (got {shop_domain!r})")
        self.url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
        self.token = token
        self.read_only = read_only
        self.log = get_logger("shopify")
        self.min_available = load_rules()["rate_limits"]["shopify_min_available_cost"]

    def execute(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.read_only:
            assert_read_only_graphql(document)
        body = {"query": document, "variables": variables or {}}
        for attempt in range(6):
            resp = http_json("POST", self.url, headers={"X-Shopify-Access-Token": self.token}, body=body)
            errors = resp.get("errors") or []
            throttled = any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors if isinstance(e, dict))
            if throttled:
                time.sleep(2 ** attempt)
                continue
            if errors:
                raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(errors)[:1000]}")
            self._respect_cost(resp.get("extensions", {}).get("cost"))
            return resp["data"]
        raise RuntimeError("Shopify GraphQL: throttled after 6 attempts")

    def _respect_cost(self, cost: dict[str, Any] | None) -> None:
        if not cost:
            return
        status = cost.get("throttleStatus") or {}
        available = status.get("currentlyAvailable")
        restore = status.get("restoreRate") or 50
        if available is not None and available < self.min_available:
            wait = (self.min_available - available) / restore
            self.log.debug("Shopify cost budget low (%s); sleeping %.1fs", available, wait)
            time.sleep(wait)

    def paginate(self, document: str, path: Iterable[str], variables: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Follow `pageInfo { hasNextPage endCursor }` for the connection at `path`."""
        out: list[dict[str, Any]] = []
        vars_ = dict(variables or {})
        vars_["cursor"] = None
        while True:
            data = self.execute(document, vars_)
            node = data
            for key in path:
                node = node[key]
            out.extend(edge["node"] for edge in node["edges"])
            if not node["pageInfo"]["hasNextPage"]:
                return out
            vars_["cursor"] = node["pageInfo"]["endCursor"]


def shopify_client_from_env(*, read_only: bool = True) -> ShopifyGraphQL:
    load_env()
    domain = require_env("SHOPIFY_SHOP_DOMAIN")["SHOPIFY_SHOP_DOMAIN"]
    version = os.environ.get("SHOPIFY_API_VERSION", "2025-07")
    if read_only:
        token = require_env("SHOPIFY_ADMIN_READ_TOKEN")["SHOPIFY_ADMIN_READ_TOKEN"]
    else:
        if os.environ.get("WPC_MODE", "plan") != "apply" or os.environ.get("WPC_HEADLESS") == "1":
            raise MutationBlockedError("Write client requested while WPC_MODE != apply (or in headless mode). Refusing.")
        # The write token lives in a separate file that the weekly runner never loads.
        load_env(project_root() / ".env.apply")
        token = require_env("SHOPIFY_ADMIN_WRITE_TOKEN")["SHOPIFY_ADMIN_WRITE_TOKEN"]
    return ShopifyGraphQL(domain, token, version, read_only=read_only)


def money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip().replace("$", "").replace(",", "")
    mult = 1.0
    if text.lower().endswith("m"):
        mult, text = 1_000_000.0, text[:-1]
    elif text.lower().endswith("k"):
        mult, text = 1_000.0, text[:-1]
    try:
        return round(float(text) * mult, 2)
    except ValueError:
        return None


def charm_price(value: float) -> float:
    """Round UP to the next .99 ending (e.g. 26.10 -> 26.99, 27.00 -> 27.99)."""
    import math
    whole = math.floor(value)
    candidate = whole + 0.99
    if candidate + 1e-9 < value:
        candidate = whole + 1.99
    return round(candidate, 2)
