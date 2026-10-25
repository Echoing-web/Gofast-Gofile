"""GoFile API client (SPEC 4.12): account, content, token cache.

Guest tokens are persisted and reused across runs (community-researched
requirement: frequent guest-account creation is a known IP-ban trigger).
API JSON errors map onto the SPEC 4.2 taxonomy, including error-rateLimit
(Throttled) and error-unavailable (FatalError with salt-rotation hint).
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from . import atomicio
from .admission import AdmissionController, run_guarded
from .consts import API_BASE, UA
from .errors import FatalError, Throttled

DEFAULT_SALT = "12af056dacea0b"


def website_token(account_token: str, salt: str,
                  now_fn: Callable[[], float] = time.time) -> str:
    slot = int(now_fn()) // 14400
    raw = f"{UA}::en-US::{account_token}::{slot}::{salt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def flatten(data: dict) -> list[dict]:
    if data.get("type") == "file":
        return [data]
    out: list[dict] = []
    for child in data.get("children", {}).values():
        if child.get("type") == "folder":
            out.extend(flatten(child))
        else:
            out.append(child)
    return out


class TokenCache:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Optional[str]:
        import json
        try:
            raw = self.path.read_text(encoding="utf-8")
            d = json.loads(raw)
        except (OSError, ValueError):
            return None
        token = d.get("token") if isinstance(d, dict) else None
        return token if isinstance(token, str) and token else None

    def save(self, token: str) -> None:
        import json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomicio.atomic_write_text(
            str(self.path.parent), self.path.name,
            json.dumps({"token": token}, ensure_ascii=False),
        )


class GoFileApi:
    def __init__(
        self,
        transport,
        admission: AdmissionController,
        salt: Optional[str] = None,
        api_base: str = API_BASE,
        now_fn: Callable[[], float] = time.time,
        token_cache: Optional[TokenCache] = None,
    ):
        self.transport = transport
        self.admission = admission
        self.salt = salt or os.environ.get("GOFILE_WT_SALT", DEFAULT_SALT)
        self.api_base = api_base.rstrip("/")
        self.now_fn = now_fn
        self.token_cache = token_cache

    # ----------------------------------------------------------------- tokens
    def ensure_token(self, explicit_token: Optional[str] = None) -> str:
        if explicit_token:
            return explicit_token
        cached = self.token_cache.load() if self.token_cache else None
        if cached:
            return cached
        token = self.create_account()
        if self.token_cache:
            self.token_cache.save(token)
        return token

    def invalidate_token(self) -> None:
        if self.token_cache:
            try:
                self.token_cache.path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ calls
    def create_account(self) -> str:
        headers = {
            "X-Website-Token": website_token("", self.salt, self.now_fn),
            "X-BL": "en-US",
        }
        j = run_guarded(
            self.admission,
            lambda: self.transport.json_request(
                "POST", f"{self.api_base}/accounts", headers=headers),
        )
        self._check_ok(j, "account creation")
        try:
            return str(j["data"]["token"])
        except (KeyError, TypeError) as e:
            raise FatalError(f"account creation response missing token: {j}") from e

    def get_content(self, cid: str, token: str, password: Optional[str] = None) -> dict:
        url = (f"{self.api_base}/contents/{quote(cid)}"
               "?cache=true&sortField=createTime&sortDirection=1")
        if password:
            url += "&password=" + quote(password)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Website-Token": website_token(token, self.salt, self.now_fn),
            "X-BL": "en-US",
        }
        j = run_guarded(
            self.admission,
            lambda: self.transport.json_request("GET", url, headers=headers),
        )
        self._check_ok(j, f"content {cid}")
        try:
            return j["data"]
        except (KeyError, TypeError) as e:
            raise FatalError(f"content response missing data: {j}") from e

    def get_content_auto(self, cid: str, password: Optional[str] = None,
                         explicit_token: Optional[str] = None) -> "tuple[str, dict]":
        """get_content with one automatic guest-token rebuild on rejection.

        A cached guest token can be rejected (401 / error-unavailable). Per
        SPEC 4.12 we then invalidate the cache, create a fresh account once,
        and retry; a second failure propagates (likely a rotated salt).
        """
        token = self.ensure_token(explicit_token)
        try:
            return token, self.get_content(cid, token, password)
        except FatalError:
            if explicit_token or self.token_cache is None:
                raise
            if self.token_cache.load() is None:
                raise  # token wasn't from the cache; nothing to rebuild from
            self.invalidate_token()
            token = self.ensure_token()
            return token, self.get_content(cid, token, password)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _check_ok(j: dict, ctx: str) -> None:
        if not isinstance(j, dict):
            raise FatalError(f"unexpected API payload ({ctx}): {j!r}")
        if j.get("status") == "ok":
            return
        code = ""
        data = j.get("data")
        if isinstance(data, dict):
            code = str(data.get("error", ""))
        if code == "error-rateLimit":
            raise Throttled(f"GoFile API rate limit ({ctx})")
        if code == "error-unavailable":
            raise FatalError(
                f"GoFile API unavailable ({ctx}); the X-Website-Token salt may "
                f"have rotated - override it with the GOFILE_WT_SALT env var")
        raise FatalError(f"GoFile API error ({ctx}): {j}")
