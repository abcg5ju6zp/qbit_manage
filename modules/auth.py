"""Authentication module for qBittorrent-Manage web UI"""
import base64
import hashlib
import ipaddress
import os
import platform
import re
import secrets
import tempfile
import threading
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import replace as replace_dataclass
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import argon2
import ruamel.yaml
from fastapi import Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from pydantic import validator
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from modules import util


class _LoggerProxy:
    def __getattr__(self, name):
        return getattr(util.logger, name)


logger = _LoggerProxy()

# Rate limiter for authentication attempts - using in-memory storage for simplicity
# This tracks failed authentication attempts per IP address

# Simple in-memory rate limiting for authentication attempts
auth_attempts = defaultdict(list)  # IP -> list of attempt timestamps
MAX_AUTH_ATTEMPTS = 10
AUTH_WINDOW_MINUTES = 1


# Authentication models
class LoginRequest(BaseModel):
    """Login request model."""

    username: str
    password: str


class AuthSettings(BaseModel):
    """Authentication settings model."""

    enabled: bool = False
    method: str = "none"  # none, basic, api_only
    bypass_auth_for_local: bool = False  # Allow access from RFC 1918 private IPs without authentication
    trusted_proxies: list[str] = []  # List of trusted proxy IPs/subnets (e.g., ["127.0.0.1", "172.17.0.0/16"])
    username: str = ""
    password_hash: str = ""
    api_key: str = ""
    # Opaque generation token of the on-disk settings. Every successful change mints a
    # new token; saves pin the token they read and may only commit against the same one.
    settings_version: str = ""


class AuthResponse(BaseModel):
    """Authentication response model."""

    authenticated: bool
    method: str = ""
    message: str = ""


class SecuritySettingsRequest(BaseModel):
    """Security settings update request model."""

    enabled: bool
    method: str
    bypass_auth_for_local: bool = False
    trusted_proxies: list[str] = []
    username: str = ""
    password: str = ""
    generate_api_key: bool = False
    clear_api_key: bool = False

    # Generation token pinned from GET /security; a non-empty stale token rejects the save
    settings_version: str = ""

    # Current credentials for reauthentication
    current_username: str = ""
    current_password: str = ""
    current_api_key: str = ""

    @validator("username")
    def username_must_be_valid(cls, v):
        if v and (len(v) < 3 or len(v) > 50):
            raise ValueError("Username must be 3-50 characters")
        if v and not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError("Username can only contain letters, numbers, underscores, and hyphens")
        return v

    @validator("password")
    def password_must_be_strong(cls, v):
        if v:
            if len(v) < 8:
                raise ValueError("Password must be at least 8 characters")

            # Check for character type requirements
            has_upper = bool(re.search(r"[A-Z]", v))
            has_lower = bool(re.search(r"[a-z]", v))
            has_number = bool(re.search(r"\d", v))
            has_special = bool(re.search(r"[!@#$%^&*]", v))

            # Count how many character types are present
            type_count = sum([has_upper, has_lower, has_number, has_special])

            if type_count < 3:
                raise ValueError(
                    "Password must contain at least 3 of: uppercase letters, "
                    "lowercase letters, numbers, special characters (!@#$%^&*)"
                )

        return v

    @validator("method")
    def method_must_be_valid(cls, v):
        if v not in ["none", "basic", "api_only"]:
            raise ValueError("Method must be one of: none, basic, api_only")
        return v


# Authentication utilities
def hash_password(password: str) -> str:
    """Hash a password using Argon2."""
    ph = argon2.PasswordHasher()
    return ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash."""
    ph = argon2.PasswordHasher()
    try:
        ph.verify(hashed, password)
        return True
    except argon2.exceptions.VerifyMismatchError:
        return False


def generate_api_key() -> str:
    """Generate a secure API key."""
    return secrets.token_urlsafe(32)


def verify_api_key(api_key: str, stored_key: str) -> bool:
    """Verify an API key."""
    return secrets.compare_digest(api_key, stored_key)


def is_rate_limited(request: Request) -> bool:
    """Check if the client IP is rate limited for authentication attempts."""
    client_ip = request.client.host if request.client else "unknown"
    now = datetime.now()

    # Clean old attempts (older than the window)
    cutoff_time = now - timedelta(minutes=AUTH_WINDOW_MINUTES)
    auth_attempts[client_ip] = [attempt_time for attempt_time in auth_attempts[client_ip] if attempt_time > cutoff_time]

    # Check if rate limited
    return len(auth_attempts[client_ip]) >= MAX_AUTH_ATTEMPTS


def record_auth_attempt(request: Request):
    """Record a failed authentication attempt."""
    client_ip = request.client.host if request.client else "unknown"
    auth_attempts[client_ip].append(datetime.now())


def authenticate_user(username: str, password: str, settings: AuthSettings) -> bool:
    """Authenticate a user with username and password."""
    if not settings.username or not settings.password_hash:
        return False

    if username != settings.username:
        return False

    return verify_password(password, settings.password_hash)


def get_real_client_ip(request: Request, trusted_proxies: list[str] = None) -> str:
    """Get the real client IP, considering trusted proxies."""
    if not trusted_proxies:
        trusted_proxies = []

    # Get the immediate client IP
    immediate_ip = request.client.host if request.client else "unknown"

    # If immediate IP is unknown, try to infer from Host header for localhost access
    if immediate_ip == "unknown":
        host_header = request.headers.get("host", "").split(":")[0]
        if host_header in ["localhost", "127.0.0.1", "::1"]:
            immediate_ip = "127.0.0.1"

    # If no trusted proxies configured, return immediate IP
    if not trusted_proxies:
        return immediate_ip

    # Check if immediate client is a trusted proxy
    is_trusted_proxy = False
    try:
        immediate_ip_obj = ipaddress.ip_address(immediate_ip)

        for proxy in trusted_proxies:
            try:
                if "/" in proxy:  # CIDR notation
                    network = ipaddress.ip_network(proxy, strict=False)
                    if immediate_ip_obj in network:
                        is_trusted_proxy = True
                        break
                else:  # Single IP
                    if immediate_ip_obj == ipaddress.ip_address(proxy):
                        is_trusted_proxy = True
                        break
            except (ipaddress.AddressValueError, ValueError):
                continue
    except (ipaddress.AddressValueError, ValueError):
        pass

    # If immediate client is not a trusted proxy, return immediate IP
    if not is_trusted_proxy:
        return immediate_ip

    # Extract real IP from headers (in order of preference)
    headers_to_check = [
        "CF-Connecting-IP",  # Cloudflare
        "X-Real-IP",  # Nginx
        "X-Forwarded-For",  # Standard proxy header
    ]

    for header in headers_to_check:
        header_value = request.headers.get(header)
        if header_value:
            # X-Forwarded-For can contain multiple IPs, take the first (original client)
            real_ip = header_value.split(",")[0].strip()
            try:
                # Validate the IP
                ipaddress.ip_address(real_ip)
                return real_ip
            except (ipaddress.AddressValueError, ValueError):
                continue

    # If no valid IP found in headers, return immediate IP
    return immediate_ip


def is_local_ip(request: Request, trusted_proxies: list[str] = None) -> bool:
    """Check if request is from localhost or RFC 1918 private IP ranges."""
    real_ip = get_real_client_ip(request, trusted_proxies)

    # Check localhost addresses
    if real_ip in ["127.0.0.1", "localhost", "::1"]:
        return True

    # If IP detection failed and Host header indicates localhost, treat as local
    if real_ip == "unknown":
        host_header = request.headers.get("host", "").split(":")[0]
        if host_header in ["localhost", "127.0.0.1", "::1"]:
            return True

    # Check RFC 1918 private IP ranges
    try:
        ip = ipaddress.ip_address(real_ip)
        # RFC 1918 private IP ranges
        private_ranges = [
            ipaddress.ip_network("10.0.0.0/8"),  # 10.0.0.0 - 10.255.255.255
            ipaddress.ip_network("172.16.0.0/12"),  # 172.16.0.0 - 172.31.255.255
            ipaddress.ip_network("192.168.0.0/16"),  # 192.168.0.0 - 192.168.255.255
        ]

        return any(ip in network for network in private_ranges)
    except (ipaddress.AddressValueError, ValueError):
        # If IP parsing fails, assume it's not local
        return False


@dataclass(frozen=True)
class _AuthSnapshot:
    """Immutable generation of authentication settings shared by all middleware instances."""

    settings: AuthSettings
    file_hash: str | None
    loaded_at: datetime

    @property
    def version(self) -> str:
        return self.settings.settings_version


class AuthSettingsConflict(Exception):
    """Raised when a save pins a settings generation that no longer matches the file."""


class _AuthSettingsStore:
    """Process-wide holder for one settings file's current immutable snapshot.

    All middleware instances reading the same path share one store, so a generation
    switch is a single reference assignment: Basic, API-only, local-bypass and trusted
    proxy decisions can never observe a mix of two generations. Failed reloads keep
    the last known-good snapshot; the file watcher retries until instances converge
    to whatever generation the persistent file contains.
    """

    cache_duration = timedelta(seconds=1)

    def __init__(self, settings_path: Path):
        self.settings_path = Path(settings_path)
        self._lock = threading.Lock()
        self._snapshot: _AuthSnapshot | None = None

    def current(self) -> AuthSettings:
        """Return the settings of the current generation, reloading if the file changed."""
        now = datetime.now()
        snapshot = self._snapshot
        if snapshot is not None and now - snapshot.loaded_at < self.cache_duration:
            return snapshot.settings

        with self._lock:
            snapshot = self._snapshot
            now = datetime.now()
            if snapshot is not None and now - snapshot.loaded_at < self.cache_duration:
                return snapshot.settings

            file_hash = self._hash_file()
            if snapshot is not None and file_hash is not None and file_hash == snapshot.file_hash:
                self._snapshot = replace_dataclass(snapshot, loaded_at=now)
                return snapshot.settings

            return self._reload_locked(file_hash).settings

    def force_reload(self) -> AuthSettings:
        """Reload from the persistent file and publish its generation."""
        with self._lock:
            return self._reload_locked(self._hash_file()).settings

    def publish(self, settings: AuthSettings, file_hash: str | None = None) -> None:
        """Atomically switch every consumer of this store to the given generation."""
        if file_hash is None:
            file_hash = self._hash_file()
        with self._lock:
            self._snapshot = _AuthSnapshot(settings=settings, file_hash=file_hash, loaded_at=datetime.now())

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None

    def snapshot(self) -> _AuthSnapshot | None:
        return self._snapshot

    def _reload_locked(self, file_hash: str | None) -> _AuthSnapshot:
        settings = self._read_settings_file()
        if settings is None:
            # Preserve the old generation and its validation state on read/parse failure.
            if self._snapshot is not None:
                self._snapshot = replace_dataclass(self._snapshot, loaded_at=datetime.now())
                return self._snapshot
            settings = AuthSettings()

        self._snapshot = _AuthSnapshot(settings=settings, file_hash=file_hash, loaded_at=datetime.now())
        return self._snapshot

    def _hash_file(self) -> str | None:
        try:
            if self.settings_path.exists():
                with open(self.settings_path, "rb") as f:
                    return hashlib.sha256(f.read()).hexdigest()
        except OSError:
            pass
        return None

    def _read_settings_file(self) -> AuthSettings | None:
        """Read and validate the file, repairing overly permissive modes. None on failure."""
        try:
            if not self.settings_path.exists():
                return AuthSettings()

            if platform.system() != "Windows":
                try:
                    stat_info = self.settings_path.stat()
                    if stat_info.st_mode & 0o077:
                        logger.warning(
                            f"Settings file {self.settings_path} has overly permissive permissions. "
                            f"Fixing to 600 (owner read/write only)."
                        )
                        try:
                            os.chmod(str(self.settings_path), 0o600)
                            logger.info(f"Fixed permissions for settings file {self.settings_path} to 600.")
                        except OSError as e:
                            logger.error(f"Could not fix permissions for settings file: {e}")
                except (OSError, AttributeError) as e:
                    logger.warning(f"Could not check permissions for settings file: {e}")
            else:
                logger.debug("Skipping file permission check on Windows (uses different permission model)")

            with open(self.settings_path, encoding="utf-8") as f:
                yaml_loader = ruamel.yaml.YAML(typ="safe")
                data = yaml_loader.load(f) or {}

            return AuthSettings(**(data.get("authentication") or {}))
        except Exception as e:
            logger.error(f"Error loading authentication settings: {e}")
            return None


_settings_stores: dict[str, _AuthSettingsStore] = {}
_settings_stores_lock = threading.Lock()


def get_auth_store(settings_path: Path) -> _AuthSettingsStore:
    """Return the shared store for a settings path, one store per resolved file."""
    key = str(Path(settings_path).resolve())
    store = _settings_stores.get(key)
    if store is None:
        with _settings_stores_lock:
            store = _settings_stores.get(key)
            if store is None:
                store = _AuthSettingsStore(Path(settings_path))
                _settings_stores[key] = store
    return store


def force_reload_all_stores() -> list[AuthSettings]:
    """Reload every known store from disk and return one settings object per store."""
    with _settings_stores_lock:
        stores = list(_settings_stores.values())
    return [store.force_reload() for store in stores]


def invalidate_all_stores() -> None:
    with _settings_stores_lock:
        stores = list(_settings_stores.values())
    for store in stores:
        store.invalidate()


def new_settings_version() -> str:
    """Mint an opaque generation token for a successful settings commit."""
    return secrets.token_hex(16)


def _read_raw_settings(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    with open(settings_path, encoding="utf-8") as f:
        yaml_loader = ruamel.yaml.YAML(typ="safe")
        return yaml_loader.load(f) or {}


def _fsync_directory(directory: Path) -> None:
    if platform.system() == "Windows":
        return
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _atomic_write_settings(settings_path: Path, data: dict) -> None:
    """Write the full YAML document via a durable temp file and atomic replace.

    The target file is never observed half-written and a failed write leaves the
    previous file (and thus the previous generation) in place.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{settings_path.name}.", suffix=".tmp", dir=str(settings_path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml = ruamel.yaml.YAML()
            yaml.indent(mapping=2, sequence=4, offset=2)
            yaml.dump(data, f)
            f.flush()
            os.fsync(f.fileno())

        # Restrict the temp file before it becomes the target, so the replacement
        # is never readable by group/other, not even for an instant.
        if platform.system() != "Windows":
            os.chmod(tmp_path, 0o600)

        os.replace(tmp_path, settings_path)
        tmp_path = None
        _fsync_directory(settings_path.parent)
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()


def persist_auth_settings_locked(settings_path: Path, settings: AuthSettings) -> AuthSettings:
    """Persist a complete authentication section while the caller holds the file lock.

    Re-reads the full document inside the lock so unrelated sections (e.g. schedule)
    survive, mints a generation token, writes atomically and publishes the new
    generation to every middleware consumer.
    """
    data = _read_raw_settings(settings_path)
    # Every changed document defines a new generation; never carry the pinned token.
    settings.settings_version = new_settings_version()
    data["authentication"] = settings.model_dump()
    _atomic_write_settings(settings_path, data)
    get_auth_store(settings_path).publish(settings)
    return settings


def commit_auth_settings(
    settings_path: Path, expected_version: str, settings: AuthSettings
) -> AuthSettings:
    """Compare-and-swap a full settings document against its pinned generation.

    Raises AuthSettingsConflict when the file has advanced to another generation.
    """
    with util.settings_file_lock(settings_path):
        current = load_auth_settings(settings_path)
        if expected_version and expected_version != (current.settings_version or ""):
            raise AuthSettingsConflict(
                "Security settings were changed in another session; reload and try again."
            )
        return persist_auth_settings_locked(Path(settings_path), settings)


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Authentication middleware for qBit Manage web UI."""

    def __init__(self, app, settings_path: Path, base_url: str = ""):
        super().__init__(app)
        self.settings_path = Path(settings_path)
        self.base_url = base_url
        # Touch the shared store so all instances of this path serve one generation.
        get_auth_store(self.settings_path)

    def _load_auth_settings(self) -> AuthSettings:
        """Return the current shared generation for this settings file."""
        return get_auth_store(self.settings_path).current()

    def clear_cache(self):
        """Force the shared snapshot to be considered stale."""
        get_auth_store(self.settings_path).invalidate()
        logger.trace("Authentication cache cleared")

    def force_reload_settings(self) -> AuthSettings:
        """Force the shared store to reload the file generation."""
        return get_auth_store(self.settings_path).force_reload()

    @classmethod
    def clear_all_caches(cls):
        """Invalidate the shared snapshot of every settings file."""
        invalidate_all_stores()

    @classmethod
    def force_reload_all_settings(cls) -> list[AuthSettings]:
        """Reload the persistent generation of every settings file."""
        return force_reload_all_stores()

    async def dispatch(self, request: Request, call_next):
        """Process authentication for each request."""
        try:
            settings = self._load_auth_settings()

            # Skip authentication for certain paths
            base_api_path = f"{self.base_url}/api" if self.base_url else "/api"
            base_static_path = f"{self.base_url}/static" if self.base_url else "/static"
            skip_auth_paths = [
                base_static_path,
                f"{base_api_path}/health",
                f"{base_api_path}/version",
                f"{base_api_path}/get_base_url",
                "/site.webmanifest",
            ]

            if any(request.url.path.startswith(path) for path in skip_auth_paths):
                return await call_next(request)

            # Check if authentication is required
            auth_required = False
            if settings.enabled:
                if not settings.bypass_auth_for_local or not is_local_ip(request, settings.trusted_proxies):
                    auth_required = True

            if not auth_required:
                return await call_next(request)

            # Handle different authentication methods
            if settings.method == "basic":
                return await self._handle_basic_auth(request, call_next, settings)
            elif settings.method == "api_only":
                return await self._handle_api_only_auth(request, call_next, settings)
            else:
                # No authentication required
                return await call_next(request)

            # This should never be reached, but just in case
            logger.error("Auth middleware reached end without returning - this should not happen")
            return await call_next(request)

        except RateLimitExceeded:
            # Handle rate limit exceeded

            return PlainTextResponse("Rate limit exceeded. Try again later.", status_code=429, headers={"Retry-After": "60"})

    async def _handle_basic_auth(self, request: Request, call_next, settings: AuthSettings):
        """Handle HTTP Basic authentication with API key support for API endpoints."""

        # For API endpoints, allow API key authentication
        base_api_path = f"{self.base_url}/api" if self.base_url else "/api"
        if request.url.path.startswith(base_api_path):
            api_key = request.headers.get("X-API-Key")
            if api_key and verify_api_key(api_key, settings.api_key):
                return await call_next(request)

        # For all requests (including API), require Basic authentication
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Basic "):
            # Only log as debug for repeated attempts, not initial visits
            logger.trace("Basic auth: missing or invalid authorization header")
            return self._auth_challenge_response()

        try:
            # Apply rate limiting BEFORE processing credentials
            if is_rate_limited(request):
                logger.debug("Rate limit exceeded for authentication attempt")
                return PlainTextResponse("Rate limit exceeded. Try again later.", status_code=429, headers={"Retry-After": "60"})

            # Decode the base64 encoded credentials
            encoded_credentials = auth_header.split(" ")[1]
            decoded_credentials = base64.b64decode(encoded_credentials).decode("utf-8")
            username, password = decoded_credentials.split(":", 1)

            # Verify credentials with constant-time comparison to prevent timing attacks
            if username == settings.username:
                # Username matches, verify password
                if verify_password(password, settings.password_hash):
                    return await call_next(request)
                else:
                    logger.info(f"Basic auth failed for {request.url.path}: user={username} (invalid password)")
            else:
                # Username doesn't match, but still verify password against dummy hash for constant time
                dummy_hash = (
                    "$argon2id$v=19$m=65536,t=3,p=4$pSFTuqiWsyxxjLN0fuXtrw$"
                    "MvnRW8OTPn6ED7sun2PvdxqJuYDLvog+OpEmA+Y4eSQ"
                )  # Dummy hash
                verify_password(password, dummy_hash)  # This will always fail but takes constant time
                logger.info(f"Basic auth failed for {request.url.path}: user={username} (invalid username)")

            # Record failed authentication attempt
            record_auth_attempt(request)
            return self._auth_challenge_response()

        except Exception:
            logger.debug("Basic auth: error processing credentials")
            # Record failed authentication attempt
            record_auth_attempt(request)
            return self._auth_challenge_response()

    async def _handle_api_only_auth(self, request: Request, call_next, settings: AuthSettings):
        """Handle API-only authentication."""
        # Allow all non-API requests to pass through without authentication
        base_api_path = f"{self.base_url}/api" if self.base_url else "/api"
        if not request.url.path.startswith(base_api_path):
            return await call_next(request)

        # For API requests, require API key authentication
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            logger.info(f"API-only auth failed for {request.url.path} (missing API key)")
            return PlainTextResponse(
                "API key required for API access", status_code=401, headers={"WWW-Authenticate": 'Bearer realm="qBit Manage API"'}
            )

        # Apply rate limiting BEFORE processing API key
        if is_rate_limited(request):
            logger.debug("Rate limit exceeded for API key attempt")
            return PlainTextResponse("Rate limit exceeded. Try again later.", status_code=429, headers={"Retry-After": "60"})

        if not verify_api_key(api_key, settings.api_key):
            logger.info(f"API-only auth failed for {request.url.path} (invalid API key)")
            # Record failed API key attempt
            record_auth_attempt(request)
            return PlainTextResponse(
                "Invalid API key", status_code=401, headers={"WWW-Authenticate": 'Bearer realm="qBit Manage API"'}
            )

        return await call_next(request)

    def _auth_challenge_response(self):
        """Return HTTP 401 challenge response."""
        return PlainTextResponse(
            "Authentication failed", status_code=401, headers={"WWW-Authenticate": 'Basic realm="qBit Manage"'}
        )


def load_auth_settings(settings_path: Path) -> AuthSettings:
    """Load authentication settings from qbm_settings.yml."""
    try:
        if settings_path.exists():
            with open(settings_path, encoding="utf-8") as f:
                yaml_loader = ruamel.yaml.YAML(typ="safe")
                data = yaml_loader.load(f) or {}

            auth_data = data.get("authentication", {})
            return AuthSettings(**auth_data)
    except Exception as e:
        logger.error(f"Error loading authentication settings: {e}")

    # Return default settings if loading fails
    return AuthSettings()
