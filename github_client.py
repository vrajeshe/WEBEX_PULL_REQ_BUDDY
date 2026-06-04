"""GitHub Enterprise API client for PR monitoring and interaction."""

import base64
import logging
import re
import textwrap
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

PRECOMMIT_FALLBACK_TEST_TAG = "Default"

_PRECOMMIT_AUTO_TRIGGER_RE = re.compile(
    r"\[[xX]\]\s+\*\*Enable Precommit Auto-Trigger\*\*",
    re.IGNORECASE,
)


def normalize_precommit_test_tag(tag: str) -> str:
    """Normalize user tag for ``**TEST_TAG**:`` in the PR CICD block (e.g. routing → ROUTING)."""
    t = (tag or "").strip()
    if t.lower() == PRECOMMIT_FALLBACK_TEST_TAG.lower():
        return PRECOMMIT_FALLBACK_TEST_TAG
    return t.upper()


def pr_body_precommit_auto_trigger_enabled(body: str) -> bool:
    """True when the CICD block has **Enable Precommit Auto-Trigger** checked (``[x]``)."""
    return bool(_PRECOMMIT_AUTO_TRIGGER_RE.search(body or ""))


def cicd_precommit_block(test_tag: str = "ROUTING") -> str:
    """CICD Ring 2 (Precommit) markdown block for PR descriptions."""
    tag = normalize_precommit_test_tag(test_tag)
    return textwrap.dedent(
        f"""\
        ---
        <!-- BEGIN_CICD_PRECOMMIT_SECTION - DO NOT MODIFY THIS LINE! -->
        ### CICD Ring 2 (Precommit) Configuration

        * [x] **Enable Precommit Auto-Trigger**
          * If checked, precommit pipeline will automatically trigger on new commits and PR Open/Edit

        #### Precommit Parameters
        * **TEST_TAG**: {tag}
        * **TEST_CASE** (optional):
        * **TEST_BRANCH** (optional):
        * **STREAM** (optional):

        For more information on the Precommit pipeline workflow, please refer to: [SONiC CICD - Precommit Wiki](https://github.example.com/myorg/sub-cicd/wiki/SONiC-CICD-Precommit).
        <!-- END_CICD_PRECOMMIT_SECTION - DO NOT MODIFY THIS LINE! -->
        ---


        """
    ).strip()


# Default suffix for ``raise_submodule_hash_pr`` when no repo template exists.
WHITEBOX_DEFAULT_PR_TEMPLATE_SUFFIX = cicd_precommit_block("ROUTING")

_TEST_TAG_LINE_RE = re.compile(
    r"^(\s*\*\s+\*\*TEST_TAG\*\*:\s*)(?:.*)?$",
    re.MULTILINE | re.IGNORECASE,
)
_PRECOMMIT_PARAMS_HDR_RE = re.compile(
    r"(^####\s+Precommit\s+Parameters\s*\n)",
    re.MULTILINE | re.IGNORECASE,
)


class SubmoduleBumpRow(NamedTuple):
    """One submodule bump target after resolving ``.gitmodules`` and tip SHAs."""

    sub_path: str
    sub_owner: str
    sub_repo: str
    gitmodules_branch: str
    current_sha: str
    src_branch: str
    tip_sha: str


class SubmoduleAmbiguousError(Exception):
    """Several `.gitmodules` entries matched the user's hint."""

    def __init__(self, hint: str, matches: List[Tuple[str, str, str, str]]):
        self.hint = hint
        self.matches = matches
        bits = []
        for p, _, _, br in matches:
            extra = f" (`.gitmodules` branch=`{br}`)" if (br or "").strip() else ""
            bits.append(f"`{p}`{extra}")
        super().__init__(
            f"Ambiguous submodule hint `{hint}` matches {len(matches)} entries: {', '.join(bits)}. "
            f"Refine the hint (e.g. include `path/` or `owner/repo`)."
        )


def _is_frr_submodule(sub_path: str, sub_repo: str) -> bool:
    """True for the buildimage FRR submodule (also bump ``rules/frr.mk`` / ``FRR_TAG``)."""
    p = (sub_path or "").replace("\\", "/").rstrip("/").lower()
    repo = (sub_repo or "").lower()
    return p.endswith("sonic-frr/frr") or repo == "frr"


def _patch_frr_mk_frr_tag(content: str, new_hash: str) -> str:
    """Set ``FRR_TAG`` on the first matching Makefile assignment line."""
    pattern = re.compile(r"^(FRR_TAG\s*[:?]?=\s*).*$", re.MULTILINE)
    if pattern.search(content) is None:
        raise ValueError(
            "rules/frr.mk has no FRR_TAG assignment line "
            "(expected e.g. FRR_TAG = ... or FRR_TAG := ...)."
        )
    # Do not use r"\1" + new_hash: if new_hash starts with a digit, ``\179...`` is parsed as
    # backreference ``\17`` + ``9...``, corrupting the line (seen in PRs).
    return pattern.sub(lambda m: m.group(1) + new_hash, content, count=1)


CHECK_STATE_ICONS = {
    "success": "\u2705",
    "failure": "\u274C",
    "error": "\u274C",
    "pending": "\u23F3",
    "queued": "\u23F3",
    "in_progress": "\u23F3",
    "cancelled": "\u26D4",
    "action_required": "\u26A0\uFE0F",
    "neutral": "\u26AA",
    "skipped": "\u23ED\uFE0F",
    "timed_out": "\u23F0",
    "stale": "\u26AA",
}


@dataclass
class PRCheckResult:
    name: str
    state: str
    description: str = ""
    url: str = ""


@dataclass
class PRInfo:
    owner: str
    repo: str
    number: int
    title: str = ""
    state: str = ""
    html_url: str = ""
    head_sha: str = ""
    node_id: str = ""
    merged: bool = False
    auto_merge_enabled: bool = False
    checks: list = field(default_factory=list)


def parse_pr_url(url: str) -> Optional[Tuple[str, str, int]]:
    """Extract owner, repo, and PR number from a GitHub PR URL."""
    patterns = [
        r"https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)",
        r"([^/]+)/([^/]+)/pull/(\d+)",
        r"([^/]+)/([^/]+)#(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url.strip())
        if match:
            return match.group(1), match.group(2), int(match.group(3))
    return None


class GitHubEnterpriseClient:
    """Client for GitHub Enterprise API interactions."""

    def __init__(self, token: str, base_url: str):
        self._base_url = base_url.rstrip("/")
        self._graphql_url = self._base_url.replace("/api/v3", "/api/graphql")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        })
        self._session.verify = True

    def _get(self, path: str, params: Optional[dict] = None):
        url = f"{self._base_url}{path}"
        resp = self._session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json_body: dict) -> dict:
        url = f"{self._base_url}{path}"
        resp = self._session.post(url, json=json_body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, json_body: dict) -> dict:
        url = f"{self._base_url}{path}"
        resp = self._session.put(url, json=json_body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> None:
        url = f"{self._base_url}{path}"
        resp = self._session.delete(url, timeout=30)
        resp.raise_for_status()

    def _patch(self, path: str, json_body: dict) -> dict:
        url = f"{self._base_url}{path}"
        resp = self._session.patch(url, json=json_body, timeout=60)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _github_json_messages(data: object) -> str:
        """Concatenate human-readable messages from a GitHub error JSON body."""
        if not isinstance(data, dict):
            return str(data or "")
        parts = [str(data.get("message", "")).strip()]
        for err in data.get("errors") or []:
            if isinstance(err, dict):
                parts.append(str(err.get("message", "")).strip())
            else:
                parts.append(str(err).strip())
        return " ".join(p for p in parts if p)

    def _create_branch_ref_at_sha_or_reset(
        self, owner: str, repo: str, head_branch: str, sha: str
    ) -> None:
        """Create ``refs/heads/<head_branch>`` at ``sha``, or force-reset if the ref already exists.

        Bump head names are deterministic (Jira + submodule + current gitlink prefix), so a
        repeat ``raise_hash_update`` often gets **422 Reference already exists** on
        ``POST .../git/refs``. GitHub puts that text in ``errors[].message``, not the top-level
        ``message`` (which may be only ``Validation Failed``), so we scan all parts and then
        **always** try ``PATCH`` with ``force`` on 422 from create-ref.
        """
        enc_head = quote(head_branch, safe="")
        heads_path = f"/repos/{owner}/{repo}/git/refs/heads/{enc_head}"
        try:
            self._post(
                f"/repos/{owner}/{repo}/git/refs",
                json_body={"ref": f"refs/heads/{head_branch}", "sha": sha},
            )
        except requests.exceptions.HTTPError as exc:
            if getattr(exc.response, "status_code", None) != 422:
                raise
            try:
                blob = self._github_json_messages(exc.response.json())
            except Exception:
                blob = exc.response.text or ""
            low = blob.lower()
            looks_like_exists = (
                "already exists" in low
                or "reference already exists" in low
                or "reference exists" in low
            )
            if looks_like_exists:
                logger.warning(
                    "Submodule bump: branch %s already exists on %s/%s; resetting to %s",
                    head_branch,
                    owner,
                    repo,
                    sha[:7],
                )
            else:
                logger.warning(
                    "Submodule bump: POST ref 422 for %s on %s/%s (%s); trying force reset",
                    head_branch,
                    owner,
                    repo,
                    (blob or "?")[:200],
                )
            try:
                self._patch(heads_path, json_body={"sha": sha, "force": True})
            except requests.exceptions.HTTPError as patch_exc:
                if getattr(patch_exc.response, "status_code", None) == 404:
                    raise exc from patch_exc
                raise patch_exc from exc

    @staticmethod
    def format_http_error(exc: requests.exceptions.HTTPError) -> str:
        """Summarize a failed GitHub API call (status, path, JSON or text body)."""
        resp = exc.response
        req = exc.request
        method = (getattr(req, "method", None) or "?").upper()
        full_url = getattr(req, "url", "") or ""
        try:
            api_path = urlparse(full_url).path or full_url
        except Exception:
            api_path = full_url
        code = resp.status_code if resp is not None else "?"
        reason = (resp.reason or "").strip() if resp is not None else ""
        body_hint = ""
        if resp is not None:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    msg = (data.get("message") or "").strip()
                    errs = data.get("errors")
                    if errs is not None:
                        err_s = str(errs).replace("\n", " ")
                        if len(err_s) > 400:
                            err_s = err_s[:400] + "..."
                        body_hint = f"{msg} | {err_s}".strip(" |") if msg else err_s
                    else:
                        body_hint = msg or str(data)[:400]
                else:
                    body_hint = str(data)[:400]
            except Exception:
                raw = (resp.text or "").replace("\n", " ").strip()
                body_hint = raw[:400] + ("..." if len(raw) > 400 else "")
        if not body_hint:
            body_hint = "empty body"
        line = f"{method} {api_path} -> HTTP {code}"
        if reason and reason.lower() not in body_hint.lower():
            line += f" ({reason})"
        line += f": {body_hint}"
        return line[:1200]

    def get_pr(self, owner: str, repo: str, number: int) -> PRInfo:
        data = self._get(f"/repos/{owner}/{repo}/pulls/{number}")
        return PRInfo(
            owner=owner,
            repo=repo,
            number=number,
            title=data.get("title", ""),
            state=data.get("state", ""),
            html_url=data.get("html_url", ""),
            head_sha=data.get("head", {}).get("sha", ""),
            node_id=data.get("node_id", ""),
            merged=data.get("merged", False),
            auto_merge_enabled=data.get("auto_merge") is not None,
        )

    def list_open_prs_by_author(
        self, owner: str, repo: str, author: str
    ) -> List[PRInfo]:
        """List all open PRs in a repo created by a specific user.

        Uses the search API to filter by author server-side, avoiding
        pagination issues with repos that have many open PRs.
        """
        query = f"type:pr state:open repo:{owner}/{repo} author:{author}"
        data = self._get(
            "/search/issues",
            params={"q": query, "per_page": 50, "sort": "updated"},
        )
        results = []
        for item in data.get("items", []):
            pr_number = item.get("number")
            results.append(PRInfo(
                owner=owner,
                repo=repo,
                number=pr_number,
                title=item.get("title", ""),
                state=item.get("state", ""),
                html_url=item.get("html_url", ""),
                head_sha="",
            ))
        return results

    def get_check_runs(self, owner: str, repo: str, ref: str) -> List[PRCheckResult]:
        """Fetch check runs (GitHub Actions / Checks API) for a commit."""
        try:
            data = self._get(
                f"/repos/{owner}/{repo}/commits/{ref}/check-runs",
                params={"per_page": 100},
            )
            return [
                PRCheckResult(
                    name=cr["name"],
                    state=cr.get("conclusion") or cr.get("status", "pending"),
                    description=cr.get("output", {}).get("summary", "")[:120],
                    url=cr.get("html_url", ""),
                )
                for cr in data.get("check_runs", [])
            ]
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return []
            raise

    def get_commit_statuses(
        self, owner: str, repo: str, ref: str
    ) -> List[PRCheckResult]:
        """Fetch combined commit statuses (legacy Status API)."""
        data = self._get(f"/repos/{owner}/{repo}/commits/{ref}/status")
        return [
            PRCheckResult(
                name=s["context"],
                state=s["state"],
                description=s.get("description", "")[:120],
                url=s.get("target_url", ""),
            )
            for s in data.get("statuses", [])
        ]

    def get_all_checks(self, owner: str, repo: str, ref: str) -> List[PRCheckResult]:
        """Fetch both check runs and commit statuses, deduplicated by name.

        When the same check appears in both APIs, use the check run for state
        but prefer the commit status target_url (Jenkins) over the check run
        html_url (streams-dashboard).
        """
        checks = self.get_check_runs(owner, repo, ref)
        statuses = self.get_commit_statuses(owner, repo, ref)

        status_map = {s.name: s for s in statuses}
        merged: List[PRCheckResult] = []
        seen: set = set()

        for c in checks:
            seen.add(c.name)
            s = status_map.get(c.name)
            if s and s.url:
                c.url = s.url
            merged.append(c)

        for s in statuses:
            if s.name not in seen:
                merged.append(s)
                seen.add(s.name)

        return sorted(merged, key=lambda c: c.name.lower())

    def post_comment(self, owner: str, repo: str, number: int, body: str) -> str:
        """Post a comment on the PR. Returns the comment URL."""
        data = self._post(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json_body={"body": body},
        )
        return data.get("html_url", "")

    @staticmethod
    def normalize_precommit_test_tag(tag: str) -> str:
        return normalize_precommit_test_tag(tag)

    @staticmethod
    def patch_pr_body_precommit_test_tag(body: str, test_tag: str) -> str:
        """Set ``* **TEST_TAG**: <value>`` in a PR description (CICD precommit block)."""
        value = normalize_precommit_test_tag(test_tag)
        text = (body or "").replace("\r\n", "\n")
        if _TEST_TAG_LINE_RE.search(text):
            return _TEST_TAG_LINE_RE.sub(rf"\1{value}", text)
        insert_line = f"* **TEST_TAG**: {value}\n"
        if _PRECOMMIT_PARAMS_HDR_RE.search(text):
            return _PRECOMMIT_PARAMS_HDR_RE.sub(rf"\1{insert_line}", text, count=1)
        if "BEGIN_CICD_PRECOMMIT_SECTION" in text:
            return text.rstrip() + "\n" + insert_line
        block = cicd_precommit_block(value)
        if text.strip():
            return text.rstrip() + "\n\n" + block
        return block

    def update_pr_precommit_test_tag(
        self, owner: str, repo: str, number: int, test_tag: str
    ) -> Dict[str, str]:
        """Patch PR description so CICD **TEST_TAG** matches ``test_tag``."""
        pr = self._get(f"/repos/{owner}/{repo}/pulls/{number}")
        old_body = pr.get("body") or ""
        new_body = self.patch_pr_body_precommit_test_tag(old_body, test_tag)
        value = normalize_precommit_test_tag(test_tag)
        self._patch(
            f"/repos/{owner}/{repo}/pulls/{number}",
            json_body={"body": new_body},
        )
        return {
            "html_url": pr.get("html_url", ""),
            "test_tag": value,
            "auto_trigger_enabled": pr_body_precommit_auto_trigger_enabled(new_body),
        }

    def _graphql(self, query: str) -> dict:
        resp = self._session.post(
            self._graphql_url, json={"query": query}, timeout=30
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"GraphQL errors: {body['errors']}")
        return body.get("data", {})

    def enable_auto_merge(self, node_id: str, merge_method: str = "SQUASH") -> bool:
        """Enable GitHub auto-merge via GraphQL. Returns True on success."""
        query = """
        mutation {
          enablePullRequestAutoMerge(input: {pullRequestId: "%s", mergeMethod: %s}) {
            pullRequest { autoMergeRequest { enabledAt } }
          }
        }
        """ % (node_id, merge_method)
        try:
            data = self._graphql(query)
            enabled = (
                data.get("enablePullRequestAutoMerge", {})
                .get("pullRequest", {})
                .get("autoMergeRequest") is not None
            )
            return enabled
        except Exception:
            logger.exception("Failed to enable auto-merge")
            return False

    def disable_auto_merge(self, node_id: str) -> bool:
        """Disable GitHub auto-merge via GraphQL. Returns True on success."""
        query = """
        mutation {
          disablePullRequestAutoMerge(input: {pullRequestId: "%s"}) {
            pullRequest { autoMergeRequest { enabledAt } }
          }
        }
        """ % node_id
        try:
            data = self._graphql(query)
            disabled = (
                data.get("disablePullRequestAutoMerge", {})
                .get("pullRequest", {})
                .get("autoMergeRequest") is None
            )
            return disabled
        except Exception:
            logger.exception("Failed to disable auto-merge")
            return False

    def merge_pr(self, owner: str, repo: str, number: int) -> bool:
        """Merge a PR. Returns True on success."""
        try:
            self._put(
                f"/repos/{owner}/{repo}/pulls/{number}/merge",
                json_body={"merge_method": "squash"},
            )
            return True
        except requests.exceptions.HTTPError:
            logger.exception("Failed to merge PR #%d", number)
            return False

    def backport_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        target_branch: str,
        *,
        branch_prefix: str = "backport",
    ) -> Dict[str, object]:
        """Squash-cherry-pick a PR's full diff onto ``target_branch`` and open a new PR.

        Uses the GitHub *merges* API with a temporary "sibling" commit so 3-way merge
        semantics (and conflict detection) are honored:

        1. Resolve PR head/base SHAs and the current ``target_branch`` tip.
        2. Create a temp ref at the PR's base SHA, then a sibling commit whose tree is
           the *target* branch tree but whose parent is the PR's base.
        3. Merge the PR head into that temp ref via ``POST /repos/.../merges`` — the
           resulting tree represents the cherry-pick. ``409`` means real conflicts
           (the user has to resolve manually); ``204`` means nothing to apply.
        4. Re-parent the merge tree as a single non-merge commit on top of
           ``target_branch`` and push it as ``backport/pr-<n>-<target>-<ts>``.
        5. Open a backport PR ``backport/...`` → ``target_branch``.

        Always cleans up the temp ref. Returns metadata for the Webex reply.
        """
        target_branch = (target_branch or "").strip()
        if not target_branch:
            raise ValueError("target_branch must be non-empty.")

        pr = self._get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        base_ref = (base.get("ref") or "").strip()
        head_sha = (head.get("sha") or "").strip()
        base_sha = (base.get("sha") or "").strip()
        pr_title = (pr.get("title") or "").strip() or f"PR #{pr_number}"
        pr_body = pr.get("body") or ""
        pr_html = pr.get("html_url", "")

        if not head_sha or not base_sha:
            raise ValueError(f"PR #{pr_number} is missing head/base SHAs.")
        if base_ref == target_branch:
            raise ValueError(
                f"PR #{pr_number} already targets `{target_branch}`; nothing to backport."
            )

        try:
            target_ref = self._get(
                f"/repos/{owner}/{repo}/git/refs/heads/{quote(target_branch, safe='')}"
            )
        except requests.exceptions.HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 404:
                raise ValueError(
                    f"Target branch `{target_branch}` does not exist on `{owner}/{repo}`."
                ) from exc
            raise
        target_tip = (target_ref.get("object") or {}).get("sha") or ""
        if not target_tip:
            raise ValueError(f"Could not resolve `{target_branch}` tip SHA.")

        target_commit = self._get(
            f"/repos/{owner}/{repo}/git/commits/{target_tip}"
        )
        target_tree = (target_commit.get("tree") or {}).get("sha") or ""
        if not target_tree:
            raise ValueError(f"Could not resolve tree for `{target_branch}` tip.")

        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        temp_branch = f"_bp/tmp/pr-{pr_number}-{target_branch}-{ts}"
        new_branch = f"{branch_prefix}/pr-{pr_number}-{target_branch}-{ts}"
        enc_temp = quote(temp_branch, safe="")
        enc_new = quote(new_branch, safe="")

        try:
            # Step 1: temp ref at PR base SHA.
            self._post(
                f"/repos/{owner}/{repo}/git/refs",
                json_body={"ref": f"refs/heads/{temp_branch}", "sha": base_sha},
            )

            # Step 2: sibling commit — target's tree on top of PR base.
            sibling = self._post(
                f"/repos/{owner}/{repo}/git/commits",
                json_body={
                    "message": (
                        f"backport: target snapshot for PR #{pr_number} "
                        f"(temporary, will be discarded)"
                    ),
                    "tree": target_tree,
                    "parents": [base_sha],
                },
            )
            self._patch(
                f"/repos/{owner}/{repo}/git/refs/heads/{enc_temp}",
                json_body={"sha": sibling["sha"], "force": True},
            )

            # Step 3: 3-way merge of PR head into the sibling. This is what surfaces
            # conflicts; if it succeeds, ``merge.tree`` is the cherry-picked tree.
            try:
                merge_resp = self._session.post(
                    f"{self._base_url}/repos/{owner}/{repo}/merges",
                    json={
                        "base": temp_branch,
                        "head": head_sha,
                        "commit_message": (
                            f"backport: 3-way merge for PR #{pr_number}"
                        ),
                    },
                    timeout=60,
                )
            except requests.RequestException as exc:
                raise ValueError(f"Merges API call failed: {exc}") from exc

            if merge_resp.status_code == 204:
                raise ValueError(
                    f"PR #{pr_number} head is already reachable from `{target_branch}` — "
                    f"nothing to backport."
                )
            if merge_resp.status_code == 409:
                raise ValueError(
                    "Cherry-pick has **conflicts** — backport cannot proceed automatically.\n"
                    "Resolve manually:\n"
                    "```\n"
                    f"git fetch origin pull/{pr_number}/head:pr-{pr_number}\n"
                    f"git checkout -b {new_branch} origin/{target_branch}\n"
                    f"git merge --squash pr-{pr_number}\n"
                    "# resolve conflicts, then:\n"
                    f"git commit -m 'Backport PR #{pr_number}: {pr_title}'\n"
                    f"git push origin {new_branch}\n"
                    "```"
                )
            try:
                merge_resp.raise_for_status()
            except requests.exceptions.HTTPError:
                raise
            merge_commit = merge_resp.json() or {}
            # /merges returns a *repo commit* object: {sha, commit:{tree:{sha}}, ...}.
            # The `git/commits/{sha}` endpoint instead returns the *git commit* object
            # ({tree:{sha}} at top level). Accept either shape, and as a last resort
            # re-fetch the git commit by SHA so backport never silently aborts.
            merge_tree = (
                ((merge_commit.get("commit") or {}).get("tree") or {}).get("sha")
                or (merge_commit.get("tree") or {}).get("sha")
                or ""
            )
            merge_sha = (merge_commit.get("sha") or "").strip()
            if not merge_tree and merge_sha:
                git_commit = self._get(
                    f"/repos/{owner}/{repo}/git/commits/{merge_sha}"
                )
                merge_tree = (git_commit.get("tree") or {}).get("sha") or ""
            if not merge_tree:
                raise ValueError(
                    "Merges API did not return a tree SHA; cannot continue."
                )

            # Step 4: re-parent merge tree onto target tip as a normal commit.
            backport_msg = (
                f"Backport PR #{pr_number}: {pr_title}\n\n"
                f"Cherry-picked from {pr_html or f'PR #{pr_number}'}.\n"
            )
            new_commit = self._post(
                f"/repos/{owner}/{repo}/git/commits",
                json_body={
                    "message": backport_msg,
                    "tree": merge_tree,
                    "parents": [target_tip],
                },
            )

            # Step 5: push as a fresh branch.
            self._post(
                f"/repos/{owner}/{repo}/git/refs",
                json_body={
                    "ref": f"refs/heads/{new_branch}",
                    "sha": new_commit["sha"],
                },
            )
        finally:
            try:
                self._delete(f"/repos/{owner}/{repo}/git/refs/heads/{enc_temp}")
            except Exception:
                logger.warning(
                    "backport: could not delete temp ref %s on %s/%s",
                    temp_branch,
                    owner,
                    repo,
                )

        # Step 6: open backport PR.
        new_pr_body_lines = [
            f"Backport of #{pr_number} (`{base_ref}`) into `{target_branch}`.",
            "",
            f"Original PR: {pr_html}".rstrip(),
            "",
        ]
        if pr_body:
            new_pr_body_lines.extend(["---", "", "**Original description:**", "", pr_body])

        new_pr_body = "\n".join(new_pr_body_lines).rstrip() + "\n"
        # For buildimage, mirror raise_hash_update's PR body shape:
        # backport header → repo PR template (if any) → CICD precommit block.
        if repo.lower() == "buildimage":
            new_pr_body = self._compose_raise_hash_pr_description(
                owner,
                repo,
                target_branch,
                new_pr_body,
            )

        new_pr = self._post(
            f"/repos/{owner}/{repo}/pulls",
            json_body={
                "title": f"[Backport #{pr_number} → {target_branch}] {pr_title}",
                "head": new_branch,
                "base": target_branch,
                "body": new_pr_body,
            },
        )

        return {
            "number": pr_number,
            "original_html_url": pr_html,
            "new_pr_number": new_pr.get("number"),
            "new_pr_url": new_pr.get("html_url", ""),
            "new_branch": new_branch,
            "target_branch": target_branch,
            "commit_sha": new_commit["sha"],
        }

    def update_branch(self, node_id: str, method: str = "MERGE") -> bool:
        """Update a PR branch via GraphQL. method: MERGE or REBASE. Returns True on success."""
        method = method.upper()
        if method not in ("MERGE", "REBASE"):
            raise ValueError(f"Invalid update method: {method}")
        query = """
            mutation {{
                updatePullRequestBranch(input: {{
                    pullRequestId: "{node_id}",
                    updateMethod: {method}
                }}) {{
                    pullRequest {{ id }}
                }}
            }}
        """.format(node_id=node_id, method=method)
        try:
            self._graphql(query)
            return True
        except Exception:
            logger.exception("Failed to update branch (%s) for %s", method, node_id)
            return False

    def approve_pr(self, owner: str, repo: str, number: int) -> bool:
        """Submit an APPROVE review on a PR. Returns True on success."""
        try:
            self._post(
                f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                json_body={"event": "APPROVE"},
            )
            return True
        except requests.exceptions.HTTPError:
            logger.exception("Failed to approve PR %s/%s#%d", owner, repo, number)
            return False

    def get_pr_summary(self, owner: str, repo: str, number: int) -> Dict:
        """Get a rich summary of a PR: metadata, files changed, review state."""
        pr = self._get(f"/repos/{owner}/{repo}/pulls/{number}")
        files = self._get(
            f"/repos/{owner}/{repo}/pulls/{number}/files",
            params={"per_page": 100},
        )
        reviews = self._get(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            params={"per_page": 50},
        )

        latest_reviews: Dict[str, str] = {}
        for r in reviews:
            user = r.get("user", {}).get("login", "")
            state = r.get("state", "")
            if user and state != "COMMENTED":
                latest_reviews[user] = state

        return {
            "title": pr.get("title", ""),
            "state": pr.get("state", ""),
            "user": pr.get("user", {}).get("login", ""),
            "html_url": pr.get("html_url", ""),
            "body": (pr.get("body") or "")[:500],
            "created_at": pr.get("created_at", ""),
            "updated_at": pr.get("updated_at", ""),
            "mergeable": pr.get("mergeable"),
            "mergeable_state": pr.get("mergeable_state", ""),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", 0),
            "commits": pr.get("commits", 0),
            "head_branch": pr.get("head", {}).get("ref", ""),
            "base_branch": pr.get("base", {}).get("ref", ""),
            "auto_merge": pr.get("auto_merge") is not None,
            "files": [
                {
                    "name": f.get("filename", ""),
                    "status": f.get("status", ""),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                }
                for f in files[:30]
            ],
            "file_count_truncated": len(files) > 30,
            "reviews": latest_reviews,
        }

    def unapprove_pr(self, owner: str, repo: str, number: int) -> bool:
        """Submit a REQUEST_CHANGES review to withdraw approval."""
        try:
            self._post(
                f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                json_body={
                    "event": "REQUEST_CHANGES",
                    "body": "Approval withdrawn via PR Monitor Bot.",
                },
            )
            return True
        except requests.exceptions.HTTPError:
            logger.exception("Failed to unapprove PR %s/%s#%d", owner, repo, number)
            return False

    def get_full_pr_summary(self, owner: str, repo: str, number: int) -> Dict:
        """Full PR details: metadata, files, reviews with bodies, and comments."""
        pr = self._get(f"/repos/{owner}/{repo}/pulls/{number}")
        files = self._get(
            f"/repos/{owner}/{repo}/pulls/{number}/files",
            params={"per_page": 100},
        )
        reviews = self._get(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            params={"per_page": 100},
        )
        issue_comments = self._get(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": 50},
        )
        review_comments = self._get(
            f"/repos/{owner}/{repo}/pulls/{number}/comments",
            params={"per_page": 50},
        )

        review_list = []
        for r in reviews:
            review_list.append({
                "user": r.get("user", {}).get("login", ""),
                "state": r.get("state", ""),
                "body": (r.get("body") or "")[:300],
                "submitted_at": r.get("submitted_at", ""),
            })

        comment_list = []
        for c in issue_comments:
            comment_list.append({
                "user": c.get("user", {}).get("login", "unknown"),
                "body": (c.get("body") or "")[:300],
                "created_at": c.get("created_at", ""),
                "url": c.get("html_url", ""),
            })

        inline_comments = []
        for c in review_comments:
            inline_comments.append({
                "user": c.get("user", {}).get("login", "unknown"),
                "body": (c.get("body") or "")[:200],
                "path": c.get("path", ""),
                "line": c.get("line") or c.get("original_line", ""),
                "created_at": c.get("created_at", ""),
            })

        return {
            "title": pr.get("title", ""),
            "state": pr.get("state", ""),
            "merged": pr.get("merged", False),
            "user": pr.get("user", {}).get("login", ""),
            "html_url": pr.get("html_url", ""),
            "body": (pr.get("body") or ""),
            "created_at": pr.get("created_at", ""),
            "updated_at": pr.get("updated_at", ""),
            "merged_at": pr.get("merged_at"),
            "merged_by": (pr.get("merged_by") or {}).get("login", ""),
            "mergeable": pr.get("mergeable"),
            "mergeable_state": pr.get("mergeable_state", ""),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", 0),
            "commits": pr.get("commits", 0),
            "head_branch": pr.get("head", {}).get("ref", ""),
            "base_branch": pr.get("base", {}).get("ref", ""),
            "auto_merge": pr.get("auto_merge") is not None,
            "labels": [l.get("name", "") for l in pr.get("labels", [])],
            "assignees": [a.get("login", "") for a in pr.get("assignees", [])],
            "files": [
                {
                    "name": f.get("filename", ""),
                    "status": f.get("status", ""),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                }
                for f in files
            ],
            "reviews": review_list,
            "comments": comment_list,
            "inline_comments": inline_comments,
        }

    def get_pr_diff(self, owner: str, repo: str, number: int) -> str:
        """Fetch the raw unified diff for a PR."""
        url = f"{self._base_url}/repos/{owner}/{repo}/pulls/{number}"
        resp = self._session.get(
            url,
            headers={"Accept": "application/vnd.github.v3.diff"},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.text

    def get_recent_comments(
        self, owner: str, repo: str, number: int, count: int = 5
    ) -> List[Dict]:
        """Fetch the most recent comments on the PR."""
        data = self._get(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": count, "direction": "desc"},
        )
        return [
            {
                "user": c.get("user", {}).get("login", "unknown"),
                "body": c.get("body", "")[:200],
                "created_at": c.get("created_at", ""),
                "url": c.get("html_url", ""),
            }
            for c in (data[-count:] if len(data) > count else data)
        ]

    def list_issue_comments(
        self, owner: str, repo: str, number: int, per_page: int = 100
    ) -> List[Dict]:
        """Fetch issue (PR) comments, newest first, full body for watchdog parsing."""
        data = self._get(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": min(per_page, 100)},
        )
        items = sorted(
            data,
            key=lambda c: c.get("created_at", ""),
            reverse=True,
        )
        return [
            {
                "user": c.get("user", {}).get("login", "unknown"),
                "body": c.get("body", "") or "",
                "created_at": c.get("created_at", ""),
                "url": c.get("html_url", ""),
            }
            for c in items
        ]

    @staticmethod
    def parse_github_owner_repo_from_url(url: str) -> Optional[Tuple[str, str]]:
        """Parse owner/repo from a git HTTPS or SSH URL."""
        u = (url or "").strip().rstrip("/")
        if not u:
            return None
        if u.startswith("git@"):
            m = re.match(r"git@[^:]+:([^/]+)/([^./]+?)(?:\.git)?$", u)
            if m:
                return m.group(1), m.group(2)
            return None
        parsed = urlparse(u)
        if parsed.scheme not in ("http", "https"):
            return None
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) < 2:
            return None
        repo = parts[-1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        return parts[-2], repo

    @staticmethod
    def parse_gitmodules_submodules(content: str) -> List[Dict[str, str]]:
        """Parse .gitmodules into dicts with keys path, url, branch.

        ``branch`` is the optional ``branch = ...`` line; the bump uses it first to pick
        the submodule repo tip (same idea as ``git submodule update --remote``).
        """
        items: List[Dict[str, str]] = []
        for m in re.finditer(
            r'\[submodule\s+"([^"]+)"\]\s*((?:.|\n)*?)(?=\n\[|\Z)',
            content,
        ):
            body = m.group(2)
            header_name = m.group(1).strip()
            path_line_m = re.search(r"^\s*path\s*=\s*(.+)$", body, re.MULTILINE)
            if path_line_m:
                path = path_line_m.group(1).strip().strip('"').strip("'")
            else:
                # Older or minimal `.gitmodules` without `path =` — use subsection name.
                path = header_name
            url_m = re.search(r"^\s*url\s*=\s*(\S+)", body, re.MULTILINE)
            url = url_m.group(1).strip() if url_m else ""
            branch_m = re.search(r"^\s*branch\s*=\s*(.+)$", body, re.MULTILINE)
            branch = ""
            if branch_m:
                branch = branch_m.group(1).strip().strip('"').strip("'")
            items.append({"path": path, "url": url, "branch": branch})
        return items

    @staticmethod
    def _norm_sub_path(path: str) -> str:
        return (path or "").strip().strip("/").replace("\\", "/")

    @classmethod
    def find_gitmodules_matches(
        cls, modules: Sequence[Dict[str, str]], hint: str
    ) -> List[Tuple[str, str, str, str]]:
        """Resolve a user hint to submodule(s) using `.gitmodules` paths and remote URLs.

        Rules (case-insensitive, forward slashes):

        - **Multi-segment** hint (e.g. ``sonic-frr/frr``): match if the submodule **path**
          ends with those path segments (e.g. ``src/sonic-frr/frr``), or if the hint equals
          ``owner/repo`` from the submodule **url**.
        - **Single-segment** hint: match path / repo (see implementation). When several
          submodules match weakly, only the **strongest** matches are kept (exact repo or
          path leaf over ``hint`` as a middle path segment). Repo names like ``sonic-swss``
          also match ``sonic-swss-common`` via a ``hint-`` prefix rule when no stronger match exists.

        Returns deduplicated ``(path, sub_owner, sub_repo, gitmodules_branch)`` tuples
        (``gitmodules_branch`` may be empty if ``branch =`` is not set).
        """
        hint_raw = (hint or "").strip()
        if not hint_raw:
            return []
        hint_norm = cls._norm_sub_path(hint_raw).lower()
        hint_parts = [p for p in hint_norm.split("/") if p]

        results: List[Tuple[str, str, str, str]] = []
        scored_single: List[Tuple[int, str, str, str, str]] = []
        seen: set = set()

        for m in modules:
            path = cls._norm_sub_path(m.get("path") or "")
            if not path:
                continue
            url = (m.get("url") or "").strip()
            gm_branch = (m.get("branch") or "").strip()
            parsed = cls.parse_github_owner_repo_from_url(url)
            if not parsed:
                continue
            so, sr = parsed[0], parsed[1]
            pl = path.lower()
            so_l, sr_l = so.lower(), sr.lower()
            key = (path, so, sr)
            if key in seen:
                continue

            path_segments = [p for p in pl.split("/") if p]

            if len(hint_parts) >= 2:
                matched = False
                if len(path_segments) >= len(hint_parts):
                    if path_segments[-len(hint_parts) :] == hint_parts:
                        matched = True
                if len(hint_parts) == 2 and hint_parts[0] == so_l and hint_parts[1] == sr_l:
                    matched = True
                if matched:
                    seen.add(key)
                    results.append((path, so, sr, gm_branch))
            elif len(hint_parts) == 1:
                h = hint_parts[0]
                sc = 0
                if sr_l == h:
                    sc = 100
                elif path_segments and path_segments[-1] == h:
                    sc = 90
                elif pl == h or pl.endswith("/" + h):
                    sc = 80
                elif h in path_segments:
                    sc = 50
                elif len(h) >= 4 and sr_l.startswith(h + "-"):
                    sc = 40
                elif len(h) >= 4 and path_segments and path_segments[-1].startswith(h + "-"):
                    sc = 35
                if sc:
                    seen.add(key)
                    scored_single.append((sc, path, so, sr, gm_branch))

        if len(hint_parts) >= 2:
            return results
        if len(hint_parts) == 1:
            if not scored_single:
                return []
            best: Dict[Tuple[str, str, str], Tuple[int, str, str, str, str]] = {}
            for t in scored_single:
                sc, path, so, sr, br = t
                k = (path, so, sr)
                if k not in best or sc > best[k][0]:
                    best[k] = t
            merged = list(best.values())
            mx = max(t[0] for t in merged)
            return [
                (p, so, sr, br)
                for sc, p, so, sr, br in merged
                if sc == mx
            ]
        return []

    def get_file_text(self, owner: str, repo: str, path: str, ref: str) -> str:
        """Return decoded file contents at ref (branch or SHA)."""
        enc_path = "/".join(quote(seg, safe="") for seg in path.split("/"))
        data = self._get(
            f"/repos/{owner}/{repo}/contents/{enc_path}",
            params={"ref": ref},
        )
        if isinstance(data, list):
            raise ValueError(f"Path {path} is a directory")
        enc = data.get("encoding")
        raw = data.get("content", "") or ""
        if enc == "base64":
            return base64.b64decode(raw.replace("\n", "")).decode("utf-8")
        return raw

    def get_repo_default_branch(self, owner: str, repo: str) -> str:
        data = self._get(f"/repos/{owner}/{repo}")
        return str(data.get("default_branch") or "master")

    def get_branch_tip_sha(self, owner: str, repo: str, branch: str) -> str:
        enc = quote(branch, safe="")
        data = self._get(f"/repos/{owner}/{repo}/git/ref/heads/{enc}")
        return str(data["object"]["sha"])

    def resolve_repo_ref_to_commit_sha(self, owner: str, repo: str, ref: str) -> str:
        """Resolve parent ref (branch, tag, or 40-char SHA) to a commit SHA.

        ``get_branch_tip_sha`` only follows ``refs/heads/...``. Release-style refs like
        ``202405c`` are often **tags**; the commits API accepts branch names, tags, and SHAs.
        """
        r = (ref or "").strip()
        if len(r) == 40 and re.fullmatch(r"[0-9a-fA-F]+", r):
            return r.lower()
        enc = quote(r, safe="")
        data = self._get(f"/repos/{owner}/{repo}/commits/{enc}")
        return str(data["sha"])

    def get_submodule_gitlink_sha(
        self, parent_owner: str, parent_repo: str, submodule_path: str, ref: str
    ) -> str:
        """Return the git submodule commit SHA (gitlink) at ref."""
        enc_path = "/".join(quote(seg, safe="") for seg in submodule_path.split("/"))
        data = self._get(
            f"/repos/{parent_owner}/{parent_repo}/contents/{enc_path}",
            params={"ref": ref},
        )
        if isinstance(data, list):
            raise ValueError(f"Submodule path {submodule_path} is a directory")
        if data.get("type") != "submodule":
            raise ValueError(
                f"Path {submodule_path} is not a submodule (type={data.get('type')})"
            )
        return str(data["sha"])

    def compare_commits(
        self, owner: str, repo: str, base_sha: str, head_sha: str
    ) -> Optional[dict]:
        """Three-dot compare base...head. Returns None on failure."""
        try:
            return self._get(
                f"/repos/{owner}/{repo}/compare/"
                f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
            )
        except requests.exceptions.HTTPError:
            logger.exception(
                "compare failed %s/%s %s...%s", owner, repo, base_sha[:7], head_sha[:7]
            )
            return None

    def commit_is_ancestor_of_tip(
        self, owner: str, repo: str, commit_sha: str, branch_tip_sha: str
    ) -> bool:
        """True if commit_sha is reachable from branch_tip (tip is same or ahead)."""
        comp = self.compare_commits(owner, repo, commit_sha, branch_tip_sha)
        if not comp:
            return False
        if comp.get("status") == "diverged":
            return False
        return int(comp.get("behind_by", -1)) == 0

    def find_submodule_source_branch(
        self,
        sub_owner: str,
        sub_repo: str,
        current_sha: str,
        parent_target_branch: str,
        gitmodules_branch: Optional[str] = None,
        max_branch_pages: int = 3,
    ) -> Tuple[str, str]:
        """Pick a branch whose tip contains ``current_sha``; return ``(branch_name, tip_sha)``.

        If ``gitmodules_branch`` is set (``branch =`` in ``.gitmodules`` for this submodule),
        it is tried **first** so the bump tracks the same remote branch as the parent checkout.
        """
        default_br = self.get_repo_default_branch(sub_owner, sub_repo)
        candidates: List[str] = []
        gm = (gitmodules_branch or "").strip()
        if gm:
            candidates.append(gm)
        for b in (default_br, parent_target_branch, "master", "main"):
            if b and b not in candidates:
                candidates.append(b)

        page = 1
        max_total_candidates = 40
        while page <= max_branch_pages and len(candidates) < max_total_candidates:
            data = self._get(
                f"/repos/{sub_owner}/{sub_repo}/branches",
                params={"per_page": 100, "page": page},
            )
            branches = data if isinstance(data, list) else []
            if not branches:
                break
            for br in branches:
                name = br.get("name", "")
                if name and name not in candidates:
                    candidates.append(name)
                if len(candidates) >= max_total_candidates:
                    break
            if len(branches) < 100 or len(candidates) >= max_total_candidates:
                break
            page += 1

        for br_name in candidates:
            try:
                tip = self.get_branch_tip_sha(sub_owner, sub_repo, br_name)
            except requests.exceptions.HTTPError:
                continue
            if tip == current_sha:
                return br_name, tip
            if self.commit_is_ancestor_of_tip(sub_owner, sub_repo, current_sha, tip):
                return br_name, tip

        tip = self.get_branch_tip_sha(sub_owner, sub_repo, default_br)
        return default_br, tip

    @staticmethod
    def _is_duplicate_open_pull_422(exc: requests.exceptions.HTTPError) -> bool:
        """True when ``POST .../pulls`` rejects because an open PR already has this head→base."""
        if getattr(exc.response, "status_code", None) != 422:
            return False
        try:
            data = exc.response.json()
        except Exception:
            return False
        chunks = [str(data.get("message", ""))]
        for err in data.get("errors") or []:
            if isinstance(err, dict):
                chunks.append(str(err.get("message", "")))
            else:
                chunks.append(str(err))
        return "pull request already exists" in " ".join(chunks).lower()

    def find_open_pull_for_head_into_base(
        self, owner: str, repo: str, head_branch: str, base_branch: str
    ) -> Optional[dict]:
        """Return an open PR dict for ``head`` = ``owner:head_branch`` into ``base_branch`` (best effort)."""
        head_param = f"{owner}:{head_branch}"
        for params in (
            {"state": "open", "head": head_param, "base": base_branch},
            {"state": "open", "head": head_param},
        ):
            data = self._get(f"/repos/{owner}/{repo}/pulls", params=params)
            if not isinstance(data, list):
                continue
            for pr in data:
                ref = (pr.get("head") or {}).get("ref")
                if ref == head_branch:
                    return pr
        return None

    def fetch_pull_request_template_body(
        self, owner: str, repo: str, ref: str
    ) -> str:
        """Return the first non-empty default PR template at ``ref`` (common paths).

        GitHub prepopulates the PR body from these files in the UI. The REST ``POST
        /pulls`` API does not merge them automatically, so we fetch and append so the
        org default description still appears **after** our bump summary.
        """
        candidates = (
            ".github/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/PULL_REQUEST_TEMPLATE/pull_request_template.md",
            "docs/pull_request_template.md",
            "docs/PULL_REQUEST_TEMPLATE.md",
            "pull_request_template.md",
        )
        for path in candidates:
            try:
                text = self.get_file_text(owner, repo, path, ref)
            except requests.exceptions.HTTPError as exc:
                if getattr(exc.response, "status_code", None) != 404:
                    logger.warning(
                        "PR template fetch %s/%s@%s path=%s: %s",
                        owner,
                        repo,
                        ref,
                        path,
                        exc,
                    )
                continue
            except Exception as exc:
                logger.warning("PR template fetch path=%s: %s", path, exc)
                continue
            t = (text or "").strip()
            if t:
                return t
        return ""

    @staticmethod
    def _strip_prior_submodule_bump_block(body: str) -> str:
        """Remove a leading ``raise_submodule_hash_pr`` bump block so we can replace it.

        Bump blocks start with ``### Submodule update`` and include a ``Tracking:`` line.
        """
        if not body:
            return ""
        s = body.replace("\r\n", "\n").lstrip("\n")
        if not s.startswith("### Submodule update"):
            return body.strip()
        lines = s.split("\n")
        for i, line in enumerate(lines):
            st = line.strip()
            if st.startswith("Tracking:") and "**" in st:
                return "\n".join(lines[i + 1 :]).strip()
        return s.strip()

    @staticmethod
    def _merge_raise_hash_pr_body(bump_block: str, suffix: str) -> str:
        """Put the bot bump summary first, then org template / existing description."""
        bump_block = (bump_block or "").rstrip()
        suffix = (suffix or "").strip()
        if not suffix:
            return bump_block
        return f"{bump_block}\n\n{suffix}"

    @staticmethod
    def _ensure_raise_hash_cicd_suffix(body: str) -> str:
        """Append the standard CICD precommit block if not already in ``body``."""
        body = (body or "").rstrip()
        if "BEGIN_CICD_PRECOMMIT_SECTION" in body:
            return body
        return GitHubEnterpriseClient._merge_raise_hash_pr_body(
            body, WHITEBOX_DEFAULT_PR_TEMPLATE_SUFFIX
        )

    def _compose_raise_hash_pr_description(
        self,
        parent_owner: str,
        parent_repo: str,
        target_branch: str,
        bump_block: str,
        remainder_after_strip: Optional[str] = None,
    ) -> str:
        """Submodule bump first, then repo template or reused tail, then CICD if missing."""
        bump_block = (bump_block or "").rstrip()
        file_tpl = self.fetch_pull_request_template_body(
            parent_owner, parent_repo, target_branch
        ).strip()

        if remainder_after_strip is not None:
            r = remainder_after_strip.strip()
            if r:
                base = GitHubEnterpriseClient._merge_raise_hash_pr_body(bump_block, r)
            else:
                base = (
                    GitHubEnterpriseClient._merge_raise_hash_pr_body(bump_block, file_tpl)
                    if file_tpl
                    else bump_block
                )
        else:
            base = (
                GitHubEnterpriseClient._merge_raise_hash_pr_body(bump_block, file_tpl)
                if file_tpl
                else bump_block
            )

        return GitHubEnterpriseClient._ensure_raise_hash_cicd_suffix(base)

    def _submodule_bump_row_resolve_tip(
        self,
        parent_owner: str,
        parent_repo: str,
        target_branch: str,
        sub_path: str,
        sub_owner: str,
        sub_repo: str,
        gitmodules_branch: str,
    ) -> SubmoduleBumpRow:
        current_sha = self.get_submodule_gitlink_sha(
            parent_owner, parent_repo, sub_path, target_branch
        )
        src_branch, tip_sha = self.find_submodule_source_branch(
            sub_owner,
            sub_repo,
            current_sha,
            target_branch,
            gitmodules_branch=gitmodules_branch or None,
        )
        if tip_sha == current_sha:
            raise ValueError(
                f"Submodule `{sub_path}` already at tip of `{src_branch}` ({tip_sha[:7]})."
            )
        return SubmoduleBumpRow(
            sub_path=sub_path,
            sub_owner=sub_owner,
            sub_repo=sub_repo,
            gitmodules_branch=gitmodules_branch,
            current_sha=current_sha,
            src_branch=src_branch,
            tip_sha=tip_sha,
        )

    def _submodule_bump_row_from_path(
        self,
        modules: list,
        parent_owner: str,
        parent_repo: str,
        target_branch: str,
        submodule_path: str,
    ) -> SubmoduleBumpRow:
        want = self._norm_sub_path(submodule_path)
        entry = None
        for m in modules:
            if self._norm_sub_path(m.get("path") or "").lower() == want.lower():
                entry = m
                break
        if not entry:
            raise ValueError(
                f"No submodule at path `{want}` in `.gitmodules` on `{target_branch}`."
            )
        parsed = self.parse_github_owner_repo_from_url(entry.get("url") or "")
        if not parsed:
            raise ValueError(f"Submodule `{want}` has no parseable url in `.gitmodules`.")
        sub_path, sub_owner, sub_repo = entry["path"], parsed[0], parsed[1]
        gitmodules_branch = (entry.get("branch") or "").strip()
        return self._submodule_bump_row_resolve_tip(
            parent_owner,
            parent_repo,
            target_branch,
            sub_path,
            sub_owner,
            sub_repo,
            gitmodules_branch,
        )

    def _submodule_bump_row_from_hint(
        self,
        modules: list,
        parent_owner: str,
        parent_repo: str,
        target_branch: str,
        submodule_repo_hint: str,
    ) -> SubmoduleBumpRow:
        h = submodule_repo_hint.strip()
        matches = self.find_gitmodules_matches(modules, h)
        if not matches:
            raise ValueError(
                f"No submodule matching `{h}` in `.gitmodules` on "
                f"`{target_branch}`. Try a path suffix (e.g. `sonic-frr/frr`), `owner/repo` "
                f"from the submodule remote, or the full path under the parent repo."
            )
        if len(matches) > 1:
            raise SubmoduleAmbiguousError(h, matches)
        sub_path, sub_owner, sub_repo, gitmodules_branch = matches[0]
        gitmodules_branch = (gitmodules_branch or "").strip()
        return self._submodule_bump_row_resolve_tip(
            parent_owner,
            parent_repo,
            target_branch,
            sub_path,
            sub_owner,
            sub_repo,
            gitmodules_branch,
        )

    def raise_submodule_hash_pr(
        self,
        parent_owner: str,
        parent_repo: str,
        target_branch: str,
        submodule_repo_hint: str,
        jira_no: str,
        head_branch_prefix: str = "bot/submodule-bump",
        submodule_path: Optional[str] = None,
    ) -> Dict[str, str]:
        """Bump submodule(s) in parent to latest on inferred branch(es); open one PR to ``target_branch``.

        If ``submodule_path`` is set, that exact path from ``.gitmodules`` is used.
        Otherwise ``submodule_repo_hint`` is resolved via :meth:`find_gitmodules_matches`;
        multiple matches raise :class:`SubmoduleAmbiguousError` and the caller must refine
        the hint (e.g. add ``path/`` or ``owner/repo``).

        **Comma-separated hints** (e.g. ``sonic-mgmt-common,sonic-swss``) bump each submodule
        in a **single** commit/PR. Each hint must resolve uniquely (no disambiguation across
        multiple hints in one command).
        """
        try:
            gm_text = self.get_file_text(
                parent_owner, parent_repo, ".gitmodules", target_branch
            )
        except requests.exceptions.HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 404:
                raise ValueError(
                    f"Could not read `.gitmodules` at ref `{target_branch}` on "
                    f"`{parent_owner}/{parent_repo}` (invalid ref or missing file)."
                ) from exc
            raise
        modules = self.parse_gitmodules_submodules(gm_text)

        if submodule_path:
            rows = [
                self._submodule_bump_row_from_path(
                    modules,
                    parent_owner,
                    parent_repo,
                    target_branch,
                    submodule_path,
                )
            ]
        else:
            raw = (submodule_repo_hint or "").strip()
            hints = [x.strip() for x in raw.split(",") if x.strip()]
            if not hints:
                raise ValueError("No submodule hint(s) provided.")
            rows = []
            seen_paths: set = set()
            for h in hints:
                row = self._submodule_bump_row_from_hint(
                    modules, parent_owner, parent_repo, target_branch, h
                )
                key = row.sub_path.lower()
                if key in seen_paths:
                    raise ValueError(
                        f"Duplicate submodule path `{row.sub_path}` in hints `{raw}` "
                        "(list each submodule once)."
                    )
                seen_paths.add(key)
                rows.append(row)

        return self._create_submodule_bump_pr_from_rows(
            parent_owner,
            parent_repo,
            target_branch,
            jira_no,
            rows,
            head_branch_prefix=head_branch_prefix,
        )

    def raise_all_stale_submodules_pr(
        self,
        parent_owner: str,
        parent_repo: str,
        target_branch: str,
        jira_no: str,
        head_branch_prefix: str = "bot/submodule-bump-all",
    ) -> Dict[str, object]:
        """Bump every submodule in the parent that is **behind its tracked branch tip**.

        Walks ``.gitmodules`` on ``parent_owner/parent_repo@target_branch``, and for each
        entry resolves the gitlink SHA + the source branch tip (preferring the
        ``branch =`` hint from ``.gitmodules`` when present). Submodules that are already
        at tip — or whose remote can't be resolved — are skipped. The remaining stale
        rows are bumped in **one** commit/PR by reusing the same flow as
        :meth:`raise_submodule_hash_pr`.

        Returns the same shape as :meth:`raise_submodule_hash_pr` plus a
        ``skipped`` / ``up_to_date`` summary the caller can show to the user.
        """
        try:
            gm_text = self.get_file_text(
                parent_owner, parent_repo, ".gitmodules", target_branch
            )
        except requests.exceptions.HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 404:
                raise ValueError(
                    f"Could not read `.gitmodules` at ref `{target_branch}` on "
                    f"`{parent_owner}/{parent_repo}` (invalid ref or missing file)."
                ) from exc
            raise
        modules = self.parse_gitmodules_submodules(gm_text)
        if not modules:
            raise ValueError(
                f"`.gitmodules` on `{parent_owner}/{parent_repo}@{target_branch}` has "
                f"no submodule entries to bump."
            )

        rows: List[SubmoduleBumpRow] = []
        up_to_date: List[str] = []
        skipped: List[Tuple[str, str]] = []
        seen_paths: set = set()

        for entry in modules:
            sub_path = (entry.get("path") or "").strip()
            if not sub_path:
                continue
            key = self._norm_sub_path(sub_path).lower()
            if key in seen_paths:
                continue
            seen_paths.add(key)
            try:
                row = self._submodule_bump_row_from_path(
                    modules,
                    parent_owner,
                    parent_repo,
                    target_branch,
                    sub_path,
                )
            except ValueError as exc:
                msg = str(exc)
                if "already at tip" in msg.lower():
                    up_to_date.append(sub_path)
                else:
                    skipped.append((sub_path, msg))
                continue
            except requests.exceptions.HTTPError as exc:
                # Submodule remote not reachable / branch missing — skip but report.
                skipped.append(
                    (sub_path, f"GitHub error ({getattr(exc.response, 'status_code', '?')})")
                )
                continue
            rows.append(row)

        if not rows:
            raise ValueError(
                f"No stale submodules on `{parent_owner}/{parent_repo}@{target_branch}` "
                f"({len(up_to_date)} already at tip, {len(skipped)} skipped)."
            )

        result = self._create_submodule_bump_pr_from_rows(
            parent_owner,
            parent_repo,
            target_branch,
            jira_no,
            rows,
            head_branch_prefix=head_branch_prefix,
        )
        result["all_total"] = str(len(modules))
        result["all_stale"] = str(len(rows))
        result["all_up_to_date"] = ",".join(up_to_date)
        result["all_skipped"] = "; ".join(f"{p}: {why}" for p, why in skipped)
        return result

    def _create_submodule_bump_pr_from_rows(
        self,
        parent_owner: str,
        parent_repo: str,
        target_branch: str,
        jira_no: str,
        rows: "List[SubmoduleBumpRow]",
        *,
        head_branch_prefix: str = "bot/submodule-bump",
    ) -> Dict[str, object]:
        """Shared core: take resolved bump rows and open one PR (used by both flows)."""
        if not rows:
            raise ValueError("No submodule bump rows provided.")

        multi = len(rows) > 1

        try:
            base_sha = self.resolve_repo_ref_to_commit_sha(
                parent_owner, parent_repo, target_branch
            )
        except requests.exceptions.HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 404:
                raise ValueError(
                    f"Parent ref `{target_branch}` not found on `{parent_owner}/{parent_repo}`."
                ) from exc
            raise
        base_commit = self._get(
            f"/repos/{parent_owner}/{parent_repo}/git/commits/{base_sha}"
        )
        base_tree = base_commit["tree"]["sha"]

        tree_entries: List[dict] = []
        frr_mk_updated = False
        frr_tip_sha: Optional[str] = None
        for row in rows:
            tree_entries.append(
                {
                    "path": row.sub_path,
                    "mode": "160000",
                    "type": "commit",
                    "sha": row.tip_sha,
                }
            )
            if _is_frr_submodule(row.sub_path, row.sub_repo):
                frr_mk_updated = True
                frr_tip_sha = row.tip_sha

        if frr_mk_updated and frr_tip_sha:
            try:
                mk_before = self.get_file_text(
                    parent_owner, parent_repo, "rules/frr.mk", target_branch
                )
            except requests.exceptions.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                if status == 404:
                    raise ValueError(
                        "FRR submodule bump requires `rules/frr.mk` on the parent ref; "
                        f"not found (HTTP 404) at `{target_branch}`."
                    ) from exc
                raise
            mk_after = _patch_frr_mk_frr_tag(mk_before, frr_tip_sha)
            blob = self._post(
                f"/repos/{parent_owner}/{parent_repo}/git/blobs",
                json_body={"content": mk_after, "encoding": "utf-8"},
            )
            tree_entries.append(
                {
                    "path": "rules/frr.mk",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        new_tree = self._post(
            f"/repos/{parent_owner}/{parent_repo}/git/trees",
            json_body={"base_tree": base_tree, "tree": tree_entries},
        )
        new_tree_sha = new_tree["sha"]

        slug = re.sub(r"[^a-zA-Z0-9._-]", "-", jira_no)[:40]
        repo_slug = "-".join(sorted({r.sub_repo for r in rows}))[:50].replace("/", "-")
        head_branch = (
            f"{head_branch_prefix}-{slug}-n{len(rows)}-{rows[0].current_sha[:7]}"
        ).replace("/", "-")[:120]

        commit_lines = [
            f"Update submodule {r.sub_path} to latest {r.src_branch} ({r.tip_sha[:12]})"
            for r in rows
        ]
        commit_msg = "\n".join(commit_lines) + "\n\n"
        if frr_mk_updated and frr_tip_sha:
            commit_msg += (
                f"Also set FRR_TAG in rules/frr.mk to the new submodule SHA ({frr_tip_sha[:12]}).\n\n"
            )
        commit_msg += f"Jira: {jira_no}\n"
        for r in rows:
            commit_msg += (
                f"- {r.sub_path}: {r.current_sha[:12]} \u2192 {r.tip_sha[:12]} ({r.src_branch})\n"
            )

        self._create_branch_ref_at_sha_or_reset(
            parent_owner, parent_repo, head_branch, base_sha
        )
        new_commit = self._post(
            f"/repos/{parent_owner}/{parent_repo}/git/commits",
            json_body={
                "message": commit_msg,
                "tree": new_tree_sha,
                "parents": [base_sha],
            },
        )
        new_commit_sha = new_commit["sha"]
        enc_head = quote(head_branch, safe="")
        self._patch(
            f"/repos/{parent_owner}/{parent_repo}/git/refs/heads/{enc_head}",
            json_body={"sha": new_commit_sha},
        )

        repo_names = ", ".join(sorted({r.sub_repo for r in rows}))
        title = f"{jira_no}: Bump {repo_names} submodule{'s' if multi else ''} to latest"
        if len(title) > 240:
            title = title[:237] + "..."

        if multi:
            web_base = self._base_url.replace("/api/v3", "")

            def _commit_link(owner: str, repo: str, sha: str) -> str:
                if not sha:
                    return "`(none)`"
                return f"[`{sha[:10]}`]({web_base}/{owner}/{repo}/commit/{sha})"

            pr_body = (
                f"### Submodule update ({len(rows)} submodules)\n\n"
                f"- **Parent:** `{parent_owner}/{parent_repo}` \u2192 `{target_branch}`\n\n"
                "| # | Submodule path | Repo | `.gitmodules` branch | Source branch | Previous SHA | New SHA |\n"
                "|---|---|---|---|---|---|---|\n"
            )
            for idx, r in enumerate(rows, start=1):
                gm_b = (r.gitmodules_branch or "").strip() or "_(none)_"
                pr_body += (
                    f"| {idx} "
                    f"| `{r.sub_path}` "
                    f"| `{r.sub_owner}/{r.sub_repo}` "
                    f"| `{gm_b}` "
                    f"| `{r.src_branch}` "
                    f"| {_commit_link(r.sub_owner, r.sub_repo, r.current_sha)} "
                    f"| {_commit_link(r.sub_owner, r.sub_repo, r.tip_sha)} |\n"
                )
            pr_body += "\n"
            if frr_mk_updated and frr_tip_sha:
                pr_body += (
                    f"- **`rules/frr.mk`:** `FRR_TAG` set to `{frr_tip_sha}` (FRR submodule)\n"
                )
        else:
            r = rows[0]
            gm_line = ""
            if (r.gitmodules_branch or "").strip():
                gm_line = (
                    f"- **`.gitmodules` `branch`:** `{r.gitmodules_branch}` "
                    f"(used first to resolve submodule tip)\n"
                )
            pr_body = (
                f"### Submodule update\n\n"
                f"- **Parent:** `{parent_owner}/{parent_repo}` \u2192 `{target_branch}`\n"
                f"- **Submodule path:** `{r.sub_path}`\n"
                f"- **Submodule repo:** `{r.sub_owner}/{r.sub_repo}`\n"
                f"{gm_line}"
                f"- **Source branch used:** `{r.src_branch}`\n"
                f"- **Previous SHA:** `{r.current_sha}`\n"
                f"- **New SHA:** `{r.tip_sha}`\n"
            )
            if frr_mk_updated and frr_tip_sha:
                pr_body += (
                    f"- **`rules/frr.mk`:** `FRR_TAG` set to `{r.tip_sha}` "
                    f"(same as submodule gitlink)\n"
                )
        pr_body += f"\nTracking: **{jira_no}**"

        pr_description = self._compose_raise_hash_pr_description(
            parent_owner,
            parent_repo,
            target_branch,
            pr_body,
            remainder_after_strip=None,
        )

        try:
            pr = self._post(
                f"/repos/{parent_owner}/{parent_repo}/pulls",
                json_body={
                    "title": title,
                    "head": head_branch,
                    "base": target_branch,
                    "body": pr_description,
                },
            )
        except requests.exceptions.HTTPError as exc:
            if not self._is_duplicate_open_pull_422(exc):
                raise
            logger.warning(
                "Submodule bump: open PR already exists for %s:%s → %s; reusing",
                parent_owner,
                head_branch,
                target_branch,
            )
            pr = self.find_open_pull_for_head_into_base(
                parent_owner,
                parent_repo,
                head_branch,
                target_branch,
            )
            if not pr:
                raise
            try:
                num = int(pr["number"])
                detail = self._get(
                    f"/repos/{parent_owner}/{parent_repo}/pulls/{num}"
                )
                existing = detail.get("body") or ""
                remainder = self._strip_prior_submodule_bump_block(existing)
                merged = self._compose_raise_hash_pr_description(
                    parent_owner,
                    parent_repo,
                    target_branch,
                    pr_body,
                    remainder_after_strip=remainder,
                )
                self._patch(
                    f"/repos/{parent_owner}/{parent_repo}/pulls/{num}",
                    json_body={"body": merged},
                )
            except requests.exceptions.HTTPError:
                logger.warning(
                    "Could not refresh body on existing PR #%s", pr.get("number")
                )

        first = rows[0]
        webex_lines = [
            f"  - `{r.sub_path}` (`{r.sub_owner}/{r.sub_repo}`): "
            f"`{r.current_sha[:7]}` \u2192 `{r.tip_sha[:7]}` (`{r.src_branch}`)"
            for r in rows
        ]
        webex_submodules_md = "**Submodule(s):**\n" + "\n".join(webex_lines)

        return {
            "html_url": pr.get("html_url", ""),
            "number": str(pr.get("number", "")),
            "head_branch": head_branch,
            "sub_path": first.sub_path,
            "sub_owner": first.sub_owner,
            "sub_repo": first.sub_repo,
            "old_sha": first.current_sha,
            "new_sha": first.tip_sha,
            "source_branch": first.src_branch,
            "frr_mk_updated": "1" if frr_mk_updated else "",
            "multi": "1" if multi else "",
            "sub_repos": ",".join(sorted({r.sub_repo for r in rows})),
            "sub_paths": ",".join(r.sub_path for r in rows),
            "webex_submodules_md": webex_submodules_md,
        }


def format_checks_summary(checks: List[PRCheckResult], pr_info: PRInfo) -> str:
    """Format check results into a human-readable Webex message."""
    if not checks:
        return f"**PR #{pr_info.number}**: No checks found yet."

    success = sum(1 for c in checks if c.state == "success")
    failed = sum(1 for c in checks if c.state in ("failure", "error"))
    pending = sum(1 for c in checks if c.state in ("pending", "queued", "in_progress"))
    total = len(checks)

    lines = [
        f"**PR #{pr_info.number}** \u2014 _{pr_info.title}_",
        f"\U0001F4CA **{success}/{total}** passed | "
        f"**{failed}** failed | **{pending}** pending",
        "",
    ]

    for c in checks:
        icon = CHECK_STATE_ICONS.get(c.state, "\u2753")
        link = f"[{c.name}]({c.url})" if c.url else c.name
        desc = f" \u2014 {c.description}" if c.description else ""
        lines.append(f"  {icon} {link}{desc}")

    lines.append(f"\n\U0001F517 [View PR]({pr_info.html_url})")
    return "\n".join(lines)
