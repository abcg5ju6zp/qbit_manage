"""Regression tests for generation-pinned, atomic security-settings persistence.

Covers concurrent saves from multiple tabs, compare-and-swap conflicts, atomic
file replacement, the shared in-memory generation consumed by every
AuthenticationMiddleware instance, failed-write rollback, cross-section
locking against scheduler persistence, and unchanged/initial/disabled flows.
"""

import asyncio
import hashlib
import json
import stat
import threading

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.responses import PlainTextResponse

from modules import auth as auth_mod
from modules.auth import AuthSettings
from modules.auth import AuthSettingsConflict
from modules.auth import AuthenticationMiddleware
from modules.auth import SecuritySettingsRequest
from modules.auth import commit_auth_settings
from modules.auth import get_auth_store
from modules.auth import load_auth_settings
from modules.scheduler import Scheduler
from modules.web_api import WebAPI

USERNAME = "adminuser"
PASSWORD = "Abcdef1!2"


class _FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    """Minimal Request stand-in: client.host plus a headers mapping."""

    def __init__(self, host="203.0.113.10", headers=None):
        self.client = _FakeClient(host)
        self.headers = dict(headers or {})


def make_api(tmp_path):
    api = object.__new__(WebAPI)
    object.__setattr__(api, "default_dir", str(tmp_path))
    return api


def run(coro):
    return asyncio.run(coro)


def make_request(**overrides):
    data = {
        "enabled": True,
        "method": "basic",
        "bypass_auth_for_local": False,
        "trusted_proxies": [],
        "username": USERNAME,
        "password": "",
        "settings_version": "",
        "current_username": "",
        "current_password": "",
        "current_api_key": "",
    }
    data.update(overrides)
    return SecuritySettingsRequest(**data)


def settings_path(tmp_path):
    return tmp_path / "qbm_settings.yml"


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_configured_auth(api, ip="203.0.113.1", **overrides):
    """First-time setup from a public IP: no credentials required."""
    result = run(api.update_security_settings(make_request(password=PASSWORD, **overrides), FakeRequest(ip)))
    assert result["success"] is True, result
    return result


def response_status(response):
    return getattr(response, "status_code", 200)


def response_body(response):
    if isinstance(response, JSONResponse):
        return json.loads(response.body)
    return response


def asgi_request(path="/api/anything", headers=None, client=("203.0.113.20", 54000)):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "headers": raw_headers,
        "query_string": b"",
        "client": (client[0], client[1]),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


async def passthrough(request):
    return PlainTextResponse("ok", status_code=200)


@pytest.fixture(autouse=True)
def unique_rate_limit_ips():
    """The module-level rate limiter accumulates failed attempts per IP across tests."""
    auth_mod.auth_attempts.clear()
    yield
    auth_mod.auth_attempts.clear()


def test_initial_setup_mints_version_and_get_masks_secrets(tmp_path):
    api = make_api(tmp_path)
    result = seed_configured_auth(api)

    version = result["settings_version"]
    assert version
    assert result["api_key"] is None

    path = settings_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_auth_settings(path).settings_version == version

    # Read-only queries keep masking secrets but expose the generation token.
    shown = run(api.get_security_settings())
    assert shown.password_hash == "***"
    assert shown.api_key == ""
    assert shown.settings_version == version

    status = run(api.get_security_status())
    assert status == {"has_api_key": False, "method": "basic", "enabled": True}

    assert get_auth_store(path).current().settings_version == version


def test_concurrent_tab_saves_commit_exactly_one_generation(tmp_path):
    api = make_api(tmp_path)
    version = seed_configured_auth(api)["settings_version"]

    results = []
    barrier = threading.Barrier(2)

    def save_from_tab(proxies, ip):
        request = make_request(
            settings_version=version,
            current_username=USERNAME,
            current_password=PASSWORD,
            trusted_proxies=proxies,
        )

        def worker():
            barrier.wait()
            response = asyncio.run(api.update_security_settings(request, FakeRequest(ip)))
            results.append((proxies, response))

        thread = threading.Thread(target=worker)
        thread.start()
        return thread

    t1 = save_from_tab(["10.1.0.1"], "203.0.113.2")
    t2 = save_from_tab(["10.2.0.2"], "203.0.113.3")
    t1.join()
    t2.join()

    statuses = sorted(response_status(response) for _, response in results)
    assert statuses == [200, 409]

    conflict = next(body for _, response in results if response_status(response) == 409 for body in [response_body(response)])
    assert conflict["conflict"] is True

    stored = load_auth_settings(settings_path(tmp_path))
    winner = next(proxies for proxies, response in results if response_status(response) == 200)
    loser = next(proxies for proxies, response in results if response_status(response) == 409)
    assert stored.trusted_proxies == winner
    assert stored.trusted_proxies != loser
    assert stored.settings_version != version
    winning_body = next(response_body(response) for _, response in results if response_status(response) == 200)
    assert stored.settings_version == winning_body["settings_version"]


def test_stale_token_conflicts_but_tokenless_client_still_commits(tmp_path):
    api = make_api(tmp_path)
    version = seed_configured_auth(api)["settings_version"]
    path = settings_path(tmp_path)
    before = file_sha256(path)

    stale = run(
        api.update_security_settings(
            make_request(
                settings_version="stale-generation-token",
                current_username=USERNAME,
                current_password=PASSWORD,
                trusted_proxies=["10.3.0.1"],
            ),
            FakeRequest("203.0.113.4"),
        )
    )
    assert response_status(stale) == 409
    assert response_body(stale)["conflict"] is True
    assert file_sha256(path) == before
    assert load_auth_settings(path).trusted_proxies == []

    # API clients that do not carry a token pin the generation they authenticate
    # against under the commit lock; their existing workflow keeps working.
    ok = run(
        api.update_security_settings(
            make_request(
                current_username=USERNAME,
                current_password=PASSWORD,
                trusted_proxies=["10.3.0.2"],
            ),
            FakeRequest("203.0.113.5"),
        )
    )
    assert ok["success"] is True
    assert load_auth_settings(path).trusted_proxies == ["10.3.0.2"]
    assert version != ok["settings_version"]


def test_failed_replace_keeps_old_file_generation_and_discards_new_key(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    version = seed_configured_auth(api)["settings_version"]
    path = settings_path(tmp_path)
    before = file_sha256(path)

    def boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(auth_mod.os, "replace", boom)

    response = run(
        api.update_security_settings(
            make_request(
                settings_version=version,
                current_username=USERNAME,
                current_password=PASSWORD,
                generate_api_key=True,
            ),
            FakeRequest("203.0.113.6"),
        )
    )
    body = response_body(response)
    assert body["success"] is False

    # Old file, old validation state and old generation all survive.
    assert file_sha256(path) == before
    store = get_auth_store(path)
    assert store.current().settings_version == version
    assert store.current().api_key == ""
    assert not list(tmp_path.glob("*.tmp"))

    # The minted key reached neither the file nor any response.
    assert load_auth_settings(path).api_key == ""
    assert "api_key" not in body or body.get("api_key") is None


def test_all_middleware_instances_share_one_generation_and_converge(tmp_path):
    api = make_api(tmp_path)
    version = seed_configured_auth(api)["settings_version"]
    path = settings_path(tmp_path)

    async def noop_app(scope, receive, send):
        return None

    instance_one = AuthenticationMiddleware(noop_app, path)
    instance_two = AuthenticationMiddleware(noop_app, path)
    assert get_auth_store(path) is get_auth_store(path)

    def settings_of(instance):
        return instance._load_auth_settings()

    assert settings_of(instance_one).settings_version == version
    assert settings_of(instance_two) is settings_of(instance_one)

    new_version_response = run(
        api.update_security_settings(
            make_request(
                settings_version=version,
                current_username=USERNAME,
                current_password=PASSWORD,
                trusted_proxies=["172.20.0.0/16"],
            ),
            FakeRequest("203.0.113.7"),
        )
    )
    assert new_version_response["success"] is True

    # The generation switch is a single shared assignment: no instance lags.
    after_one = settings_of(instance_one)
    after_two = settings_of(instance_two)
    assert after_one.settings_version == new_version_response["settings_version"]
    assert after_two is after_one
    assert after_one.trusted_proxies == ["172.20.0.0/16"]

    # A corrupted file must not knock instances off the last known-good generation.
    path.write_text("::: not valid yaml :::\n", encoding="utf-8")
    get_auth_store(path).force_reload()
    assert settings_of(instance_one).settings_version == new_version_response["settings_version"]
    assert settings_of(instance_two) is settings_of(instance_one)

    # A valid newer generation on disk eventually converges every instance.
    recovered = after_one.model_copy(deep=True)
    recovered.trusted_proxies = ["10.9.9.9"]
    recovered.settings_version = "recovered-generation"
    auth_mod._atomic_write_settings(path, {"authentication": recovered.model_dump()})

    AuthenticationMiddleware.force_reload_all_settings()
    assert settings_of(instance_one).settings_version == "recovered-generation"
    assert settings_of(instance_two).settings_version == "recovered-generation"

    # A fresh store (e.g. after a service restart) cold-loads the persistent truth.
    cold_store = auth_mod._AuthSettingsStore(path)
    assert cold_store.force_reload().settings_version == "recovered-generation"


def test_generated_api_key_belongs_only_to_committing_tab(tmp_path):
    api = make_api(tmp_path)
    version = seed_configured_auth(api)["settings_version"]
    path = settings_path(tmp_path)

    winner = run(
        api.update_security_settings(
            make_request(
                settings_version=version,
                current_username=USERNAME,
                current_password=PASSWORD,
                generate_api_key=True,
            ),
            FakeRequest("203.0.113.8"),
        )
    )
    assert winner["success"] is True
    new_key = winner["api_key"]
    assert new_key
    assert load_auth_settings(path).api_key == new_key

    # GET never reveals the key after the fact.
    assert run(api.get_security_settings()).api_key == ""

    loser = run(
        api.update_security_settings(
            make_request(
                settings_version=version,
                current_username=USERNAME,
                current_password=PASSWORD,
                generate_api_key=True,
            ),
            FakeRequest("203.0.113.9"),
        )
    )
    assert response_status(loser) == 409
    loser_body = response_body(loser)
    assert loser_body.get("api_key") is None
    # The losing tab's mint never replaced the committed key.
    assert load_auth_settings(path).api_key == new_key


def test_unchanged_fields_do_not_rewrite_file_or_advance_generation(tmp_path):
    api = make_api(tmp_path)
    version = seed_configured_auth(api)["settings_version"]
    path = settings_path(tmp_path)
    before = file_sha256(path)

    response = run(
        api.update_security_settings(
            make_request(
                settings_version=version,
                current_username=USERNAME,
                current_password=PASSWORD,
            ),
            FakeRequest("203.0.113.11"),
        )
    )
    assert response["success"] is True
    assert response["message"] == "No changes to save"
    assert response["settings_version"] == version
    assert file_sha256(path) == before


def test_basic_and_api_decisions_switch_generation_together(tmp_path):
    api = make_api(tmp_path)
    path = settings_path(tmp_path)
    # api_only with an existing key, no credentials UI required.
    seed = seed_configured_auth(api, ip="203.0.113.12", method="api_only", username="", enabled=True)

    current = load_auth_settings(path)
    current.api_key = "key-generation-one"
    current = commit_auth_settings(path, seed["settings_version"], current)

    app = AuthenticationMiddleware(passthrough, path)

    async def exercise():
        missing = await app.dispatch(asgi_request(headers={}), passthrough)
        old_key = await app.dispatch(asgi_request(headers={"X-API-Key": "key-generation-one"}), passthrough)
        return missing, old_key

    missing, old_key = asyncio.run(exercise())
    assert missing.status_code == 401
    assert old_key.status_code == 200

    # Commit a new generation with a rotated key and changed proxy posture.
    rotated = load_auth_settings(path).model_copy(deep=True)
    rotated.api_key = "key-generation-two"
    rotated.trusted_proxies = ["10.8.0.0/16"]
    rotated = commit_auth_settings(path, current.settings_version, rotated)
    assert rotated.settings_version != current.settings_version

    async def exercise_rotated():
        old_now = await app.dispatch(asgi_request(headers={"X-API-Key": "key-generation-one"}), passthrough)
        new_now = await app.dispatch(asgi_request(headers={"X-API-Key": "key-generation-two"}), passthrough)
        return old_now, new_now

    old_now, new_now = asyncio.run(exercise_rotated())
    assert old_now.status_code == 401
    assert new_now.status_code == 200
    assert app._load_auth_settings().trusted_proxies == ["10.8.0.0/16"]


def test_scheduler_and_auth_writes_preserve_each_others_sections(tmp_path):
    api = make_api(tmp_path)
    seed_configured_auth(api, ip="203.0.113.13")
    path = settings_path(tmp_path)

    scheduler = object.__new__(Scheduler)
    scheduler.settings_file = path
    scheduler._persistence_disabled = False

    failures = []

    def auth_writer(worker_index):
        for _ in range(10):
            for _attempt in range(30):
                current = load_auth_settings(path)
                candidate = current.model_copy(deep=True)
                candidate.trusted_proxies = [f"10.50.{worker_index}.1"]
                try:
                    commit_auth_settings(path, current.settings_version, candidate)
                    break
                except AuthSettingsConflict:
                    continue
            else:  # pragma: no cover - indicates lock starvation
                failures.append(f"auth writer {worker_index} exhausted retries")

    def schedule_writer():
        for i in range(10):
            scheduler._persistence_disabled = False
            scheduler._persist_schedule_file("minutes", 30 + i)

    threads = [threading.Thread(target=auth_writer, args=(i,)) for i in range(2)]
    threads.append(threading.Thread(target=schedule_writer))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    raw = auth_mod._read_raw_settings(path)
    assert "schedule" in raw
    assert raw["schedule"]["type"] == "minutes"
    assert raw["authentication"]["settings_version"]
    stored = load_auth_settings(path)
    assert stored.trusted_proxies in (["10.50.0.1"], ["10.50.1.1"])
    assert get_auth_store(path).force_reload().trusted_proxies == stored.trusted_proxies


def test_disabling_auth_and_missing_file_keep_existing_behavior(tmp_path):
    api = make_api(tmp_path)

    # Missing file: read endpoints answer with safe defaults.
    defaults = run(api.get_security_settings())
    assert defaults.enabled is False
    assert defaults.method == "none"
    assert defaults.settings_version == ""

    version = seed_configured_auth(api, ip="203.0.113.14")["settings_version"]

    # Authenticated disable.
    disabled = run(
        api.update_security_settings(
            make_request(
                enabled=False,
                method="none",
                username="",
                settings_version=version,
                current_username=USERNAME,
                current_password=PASSWORD,
            ),
            FakeRequest("203.0.113.15"),
        )
    )
    assert disabled["success"] is True

    # Once authentication is off, further saves without credentials keep working
    # (the existing initial/disabled-setup exemption).
    again = run(
        api.update_security_settings(make_request(enabled=False, method="none", username=""), FakeRequest("203.0.113.16"))
    )
    assert again["success"] is True


def test_commit_helper_rejects_stale_generation_directly(tmp_path):
    api = make_api(tmp_path)
    path = settings_path(tmp_path)
    first = seed_configured_auth(api, ip="203.0.113.17")

    candidate = load_auth_settings(path).model_copy(deep=True)
    candidate.trusted_proxies = ["10.7.0.1"]
    with pytest.raises(AuthSettingsConflict):
        commit_auth_settings(path, "definitely-stale", candidate)

    # Empty expectation commits (unpinned/legacy client), then mints a generation.
    candidate = load_auth_settings(path).model_copy(deep=True)
    candidate.trusted_proxies = ["10.7.0.2"]
    saved = commit_auth_settings(path, "", candidate)
    assert saved.settings_version
    assert saved.settings_version != first["settings_version"]
    assert load_auth_settings(path).trusted_proxies == ["10.7.0.2"]
