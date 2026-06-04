"""Jenkins REST API client (job metadata, last build, trigger build / buildWithParameters)."""

import logging
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

# Path segments after /job/... that are not part of the job name.
_JOB_PATH_STOP = frozenset(
    {
        "api",
        "ws",
        "configure",
        "lastbuild",
        "lastsuccessfulbuild",
        "lastfailedbuild",
        "laststablebuild",
        "lastcompletedbuild",
        "rebuild",
        "parameterized",
        "promotion",
        "scm",
    }
)


def parse_jenkins_job_url(url: str) -> Optional[Tuple[str, str]]:
    """Parse a Jenkins job URL into ``(base_url, job_path)``.

    ``base_url`` has no trailing slash. ``job_path`` uses ``/`` between nested
    job folders (e.g. ``folder/sub/Update_Golden_Code``).
    """
    raw = (url or "").strip()
    if not raw:
        return None
    if "/job/" not in raw.lower():
        return None
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return None
    base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    path = parsed.path or ""
    parts = [p for p in path.split("/") if p != ""]
    job_segments: List[str] = []
    i = 0
    while i < len(parts):
        if parts[i].lower() != "job":
            i += 1
            continue
        if i + 1 >= len(parts):
            break
        seg = unquote(parts[i + 1])
        low = seg.lower()
        if seg.isdigit():
            break
        if low in _JOB_PATH_STOP:
            break
        job_segments.append(seg)
        i += 2
    if not job_segments:
        return None
    return base, "/".join(job_segments)


def job_api_prefix(job_path: str) -> str:
    """Turn ``a/b/c`` into ``/job/a/job/b/job/c`` (URL-encoded segments)."""
    segs = [s for s in (job_path or "").strip("/").split("/") if s]
    return "".join(f"/job/{quote(s, safe='')}" for s in segs)


class JenkinsClient:
    """Minimal Jenkins HTTP client (Basic auth + optional CSRF crumb)."""

    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        api_token: Optional[str] = None,
        timeout: int = 30,
    ):
        self._base = (base_url or "").strip().rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.verify = True
        if api_token:
            user = (username or "").strip() or ""
            self._session.auth = HTTPBasicAuth(user, api_token)
        self._session.headers.setdefault("Accept", "application/json")

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base}{path}"

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = self._url(path)
        resp = self._session.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, params: Optional[dict] = None) -> requests.Response:
        url = self._url(path)
        headers = {}
        crumb = self._fetch_crumb()
        if crumb:
            field, value = crumb
            headers[field] = value
        resp = self._session.post(
            url,
            params=params or {},
            data="",
            headers=headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp

    def _fetch_crumb(self) -> Optional[Tuple[str, str]]:
        try:
            url = self._url("/crumbIssuer/api/json")
            resp = self._session.get(url, timeout=self._timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            field = str(data.get("crumbRequestField") or "Jenkins-Crumb")
            crumb = str(data.get("crumb") or "")
            if not crumb:
                return None
            return field, crumb
        except requests.exceptions.RequestException:
            logger.debug("Jenkins crumb not available", exc_info=True)
            return None

    def get_job_json(
        self,
        job_path: str,
        tree: Optional[str] = None,
    ) -> dict:
        """GET ``.../job/.../api/json`` (optionally with ``tree=`` to trim payload)."""
        prefix = job_api_prefix(job_path)
        params = {"tree": tree} if tree else None
        return self._get(f"{prefix}/api/json", params=params)

    def get_build_json(self, job_path: str, build_number: int) -> dict:
        """GET ``.../job/.../<n>/api/json``."""
        prefix = job_api_prefix(job_path)
        return self._get(f"{prefix}/{int(build_number)}/api/json")

    def build(
        self,
        job_path: str,
        parameters: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """Queue a build. Uses ``buildWithParameters`` when ``parameters`` is non-empty.

        Returns the ``Location`` header (queue item URL) when present, else ``None``.
        """
        prefix = job_api_prefix(job_path)
        if parameters:
            path = f"{prefix}/buildWithParameters"
            resp = self._post(path, params=parameters)
        else:
            path = f"{prefix}/build"
            resp = self._post(path, None)
        loc = resp.headers.get("Location")
        return loc.strip() if loc else None

    @staticmethod
    def format_http_error(exc: requests.exceptions.HTTPError) -> str:
        """Short summary for Webex / logs."""
        resp = exc.response
        bits = [f"{getattr(resp, 'status_code', '?')}"]
        if resp is not None:
            try:
                bits.append(resp.json().get("message", "")[:200])
            except Exception:
                t = (resp.text or "")[:200]
                if t:
                    bits.append(t)
        req = exc.request
        bits.append((getattr(req, "method", "?") or "?").upper())
        bits.append(getattr(req, "url", "") or "")
        return " | ".join(str(b) for b in bits if b)
