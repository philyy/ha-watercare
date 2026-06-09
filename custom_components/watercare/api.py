"""Watercare API."""

from datetime import datetime, timedelta
import aiohttp
import logging
import pytz
from typing import Any
from collections.abc import Mapping
import json
import secrets
import hashlib
import base64
import uuid
from urllib.parse import parse_qs

_LOGGER = logging.getLogger(__name__)

# Watercare is an Auckland utility; usage days are NZ calendar days. The API
# interprets request timestamps as UTC, so default windows are computed in this
# zone and converted to UTC before being sent.
_NZ_TIMEZONE = pytz.timezone("Pacific/Auckland")


class WatercareApi:
    """Define the Watercare API."""

    def __init__(self, email, password):
        """Initialise the API."""
        self._client_id = "799c26af-c35b-4010-bd04-b6a7ebdba811"
        self._redirect_uri = "msauth://nz.co.watercare/yRDm0vmCd9zdnwt1eCLGp8KfdLY%3D"
        self._url_base = "https://customerapp.api.water.co.nz/"
        self._url_token_base = (
            "https://wslpwb2cprd.b2clogin.com/tfp/wslpwb2cprd.onmicrosoft.com"
        )
        self._p = "B2C_1_sign_up_or_sign_in_mobile"

        self._email = email
        self._password = password

        self._accountNumber = None
        self._install_date = None
        self._token = None
        self._refresh_token = None
        self._refresh_token_expires_in = 0
        self._access_token_expires_in = 0

    @property
    def install_date(self):
        """Return the meter install date as an aware datetime, or None."""
        if not self._install_date:
            return None
        try:
            return datetime.fromisoformat(self._install_date)
        except (ValueError, TypeError):
            return None

    async def async_ensure_authenticated(self):
        """Authenticate if needed; return True once an account is available."""
        if not self._accountNumber:
            await self.get_refresh_token()
        return self._accountNumber is not None

    def get_setting_json(self, page: str) -> Mapping[str, Any] | None:
        """Get the settings from json result."""
        for line in page.splitlines():
            if line.startswith("var SETTINGS = ") and line.endswith(";"):
                json_string = line.removeprefix("var SETTINGS = ").removesuffix(";")
                return json.loads(json_string)
        return None

    def generate_code_verifier(self):
        """Generate code verifier for OAuth steps."""
        code_verifier = secrets.token_urlsafe(100)
        return code_verifier[:128]

    def generate_code_challenge(self, code_verifier):
        """Generate code challenge for OAuth steps."""
        code_challenge = hashlib.sha256(code_verifier.encode()).digest()
        return base64.urlsafe_b64encode(code_challenge).rstrip(b"=").decode()

    async def get_refresh_token(self):
        """Get the refresh token."""
        _LOGGER.debug("API get_refresh_token")
        jar = aiohttp.CookieJar(quote_cookie=False)
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            url = f"{self._url_token_base}/{self._p}/oAuth2/v2.0/authorize"

            code_verifier = self.generate_code_verifier()
            code_challenge = self.generate_code_challenge(code_verifier)
            client_request_id = str(uuid.uuid4())
            scope = f"{self._client_id} openid offline_access profile"

            params = {
                "response_type": "code",
                "code_challenge_method": "S256",
                "client_id": self._client_id,
                "client-request-id": client_request_id,
                "scope": scope,
                "prompt": "select_account",
                "redirect_uri": self._redirect_uri,
                "code_challenge": code_challenge,
            }

            async with session.get(url, params=params) as response:
                response_text = await response.text()

            settings_json = self.get_setting_json(response_text)
            _LOGGER.debug(f"settings_json: {settings_json}")

            trans_id = settings_json.get("transId")
            csrf = settings_json.get("csrf")

            url = f"{self._url_token_base}/{self._p}/SelfAsserted?tx={trans_id}&p={self._p}"
            payload = {
                "request_type": "RESPONSE",
                "email": self._email,
                "password": self._password,
            }
            headers = {"X-CSRF-TOKEN": csrf}

            async with session.post(url, headers=headers, data=payload) as response:
                await response.text()

            url = f"{self._url_token_base}/{self._p}/api/CombinedSigninAndSignup/confirmed"
            params = {
                "rememberMe": "false",
                "csrf_token": csrf,
                "tx": trans_id,
                "p": self._p,
            }

            headers = {}
            async with session.get(
                url, headers=headers, params=params, allow_redirects=False
            ) as response:
                if response.status not in [200, 301, 302, 307, 308]:
                    response_text = await response.text()
                    _LOGGER.error(
                        "Failed to confirm sign in. Status: %s, Response: %s",
                        response.status,
                        response_text,
                    )
                    raise ValueError(
                        f"Sign-in confirmation failed with status {response.status}"
                    )

                location = response.headers.get("Location", "")
                if not location:
                    _LOGGER.error("No Location header in response")
                    raise ValueError("No redirect location in sign-in response")

                query_params = parse_qs(location.split("?", 1)[1])
                if "error" in query_params:
                    _LOGGER.error("Error in response: %s", query_params["error"][0])
                    _LOGGER.error(
                        "Error description: %s", query_params["error_description"][0]
                    )
                    raise ValueError(
                        f"Authentication error: {query_params['error_description'][0]}"
                    )

            code = query_params["code"][0]

            url = f"{self._url_token_base}/{self._p}/oauth2/v2.0/token"
            params = {
                "client_id": self._client_id,
                "client-request-id": client_request_id,
                "client_info": 1,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "scope": scope,
            }

            headers = {}
            async with session.get(url, headers=headers, params=params) as response:
                response_data = await response.json()
                self._refresh_token = response_data.get("refresh_token")
                self._token = response_data.get("access_token")
                self._refresh_token_expires_in = response_data.get(
                    "refresh_token_expires_in"
                )
                self._access_token_expires_in = response_data.get("expires_in")

            _LOGGER.debug("Refresh token retrieved successfully.")
            await self.get_accounts()

    async def get_api_token(self):
        """Get token from the Watercare API."""
        token_data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
        }

        jar = aiohttp.CookieJar(quote_cookie=False)
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            url = f"{self._url_token_base}/{self._p}/oauth2/v2.0/token"
            async with session.post(url, data=token_data) as response:
                if response.status == 200:
                    jsonResult = await response.json()
                    self._token = jsonResult["access_token"]
                    _LOGGER.debug(f"Authenticity Token: {self._token}")
                    await self.get_accounts()
                else:
                    _LOGGER.error("Failed to retrieve the token page.")

    async def get_accounts(self):
        """Get the first account that we see."""
        headers = {"authorization": "Bearer " + (self._token or "")}
        jar = aiohttp.CookieJar(quote_cookie=False)
        async with (
            aiohttp.ClientSession(cookie_jar=jar) as session,
            session.get(self._url_base + "v1/account", headers=headers) as result,
        ):
            if result.status == 200:
                data = await result.json()
                _LOGGER.debug(f"Accounts: {data}")
                if data and isinstance(data, list) and len(data) > 0:
                    self._accountNumber = data[0].get("accountNumber")
                    self._install_date = data[0].get("installDate")
                    if self._accountNumber:
                        _LOGGER.debug(f"AccountNumber: {self._accountNumber}")
                    else:
                        _LOGGER.error("Account number not found in the response")
                else:
                    _LOGGER.error("No accounts found in the response")
            else:
                _LOGGER.error(
                    "Failed to fetch customer accounts %s", await result.text()
                )

    async def _request_usage(self, url):
        """Perform the usage GET request, returning (status, body_text|None)."""
        headers = {"authorization": "Bearer " + (self._token or "")}
        jar = aiohttp.CookieJar(quote_cookie=False)
        async with (
            aiohttp.ClientSession(cookie_jar=jar) as session,
            session.get(url, headers=headers) as response,
        ):
            if response.status == 200:
                return response.status, await response.text()
            return response.status, None

    @staticmethod
    def _to_api_timestamp(value):
        """Serialise a datetime for the API as a naive UTC ISO string.

        The API interprets request timestamps as UTC, so timezone-aware values
        are converted to UTC first. Non-datetime values pass through unchanged.
        """
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(pytz.utc).replace(tzinfo=None)
            return value.isoformat()
        return value

    async def get_data(
        self, endpoint: str, start_date: str = None, end_date: str = None
    ):
        """Get data from the API."""
        if endpoint not in [
            "halfhourly",
            "dailywithstats",
            "monthly",
            "mechanicalmonthly",
        ]:
            raise ValueError("Invalid endpoint specified")

        # These endpoints require from/to query params (otherwise the API
        # returns 400). Default to a sensible window per endpoint when the
        # caller doesn't specify one.
        default_windows = {
            "halfhourly": timedelta(days=7),
            "dailywithstats": timedelta(days=365),
            "monthly": timedelta(days=730),
        }
        if endpoint in default_windows:
            now = datetime.now(_NZ_TIMEZONE)
            if start_date is None:
                end_date = now
                # Align the window start to NZ midnight so the oldest day is a
                # complete calendar day rather than starting partway through.
                start_date = (now - default_windows[endpoint]).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            elif end_date is None:
                end_date = now

        start_date = self._to_api_timestamp(start_date)
        end_date = self._to_api_timestamp(end_date)

        # If no account number, need to authenticate first
        if not self._accountNumber:
            _LOGGER.debug("No account number found, starting authentication process")
            await self.get_refresh_token()
            if not self._accountNumber:
                _LOGGER.error("Authentication failed - no account number obtained")
                return None

        url = f"{self._url_base}v1/usage/{self._accountNumber}/{endpoint}"
        if start_date and end_date:
            url += f"?from={start_date}&to={end_date}"

        _LOGGER.debug(f"Calling API URL: {url}")

        status, data = await self._request_usage(url)

        # A 401 means the access token has expired (they are short-lived, but
        # we poll infrequently). Refresh it with the refresh token and retry;
        # if that still fails, fall back to a full re-authentication.
        if status == 401:
            _LOGGER.debug("Access token rejected (401); refreshing access token")
            await self.get_api_token()
            status, data = await self._request_usage(url)
            if status == 401:
                _LOGGER.debug("Still 401 after refresh; re-authenticating")
                await self.get_refresh_token()
                status, data = await self._request_usage(url)

        if status == 200:
            _LOGGER.debug(f"API Response status: {status}")
            _LOGGER.debug(f"API Response data length: {len(data) if data else 0}")
            return data

        _LOGGER.error(f"Could not fetch consumption: {status}")
        return None
