from __future__ import annotations

import inspect
import logging
import os
import sys
import threading
import traceback
from types import SimpleNamespace

import pytest

from src.autonomous.context import ContextUnavailableError, EmployeeMessageScope
from src.autonomous.context.lark_source import LarkEmployeeMessageSourceFactory
from src.autonomous.domain.employees import BotPrincipal


class _Vault:
    def __init__(self, secrets):
        self.secrets = secrets
        self.calls = []

    def resolve(self, credential_ref, agent_id, app_id):
        self.calls.append((credential_ref, agent_id, app_id))
        return self.secrets[credential_ref]


class _FailingAPI:
    def __init__(self, secret: str):
        self.secret = secret
        self.requests = []

    def get(self, request):
        self.requests.append(request)
        raise RuntimeError(f"upstream accidentally included {self.secret}")


def _scope(n: int) -> EmployeeMessageScope:
    return EmployeeMessageScope(
        tenant_key="tenant_1",
        agent_id=f"agt_{n}",
        bot_principal_id=f"bot_{n}",
        app_id=f"cli_{n}",
        chat_id=f"oc_{n}",
        thread_root_message_id=f"om_root_{n}",
        current_message_id=f"om_current_{n}",
    )


def _principal(n: int) -> BotPrincipal:
    return BotPrincipal(
        bot_principal_id=f"bot_{n}",
        tenant_key="tenant_1",
        agent_id=f"agt_{n}",
        app_id=f"cli_{n}",
        credential_ref=f"cred_{n}",
    )


def test_factory_resolves_distinct_employee_credentials_without_manager_fallback(caplog) -> None:
    secrets = {"cred_1": "secret-alpha", "cred_2": "secret-bravo"}
    vault = _Vault(secrets)
    builds = []
    apis = []

    def builder(*, app_id, app_secret, timeout):
        builds.append((app_id, app_secret, timeout))
        api = _FailingAPI(app_secret)
        apis.append(api)
        return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=api)))

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=builder,
        request_timeout_seconds=9.0,
    )

    with caplog.at_level(logging.DEBUG):
        sources = [factory.open(scope=_scope(n), principal=_principal(n)) for n in (1, 2)]
        for source in sources:
            with source, pytest.raises(ContextUnavailableError) as raised:
                source.resolve_thread()
            assert str(raised.value) == "CONTEXT_UNAVAILABLE:source"

    assert vault.calls == [
        ("cred_1", "agt_1", "cli_1"),
        ("cred_2", "agt_2", "cli_2"),
    ]
    assert builds == [
        ("cli_1", "secret-alpha", 9.0),
        ("cli_2", "secret-bravo", 9.0),
    ]
    assert all(source.closed for source in sources)

    exposed = "\n".join(
        [
            repr(factory),
            *(repr(source) for source in sources),
            caplog.text,
            repr([vars(request) for api in apis for request in api.requests]),
            repr(sys.argv),
            repr(dict(os.environ)),
        ]
    )
    for secret in secrets.values():
        assert secret not in exposed


def test_factory_rejects_projection_scope_mismatch_before_vault_resolution() -> None:
    vault = _Vault({"cred_1": "secret-alpha"})
    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=lambda **_: pytest.fail("client must not be built"),
    )

    with pytest.raises(ContextUnavailableError) as raised:
        with factory.open(scope=_scope(1), principal=_principal(2)):
            pytest.fail("mismatched lease must not open")

    assert str(raised.value) == "CONTEXT_UNAVAILABLE:credentials"
    assert vault.calls == []


def test_factory_sanitizes_client_build_failure_and_does_not_cache_secret() -> None:
    vault = _Vault({"cred_1": "secret-alpha"})

    def fail_build(**_):
        raise RuntimeError("builder exposed secret-alpha")

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=fail_build,
    )
    with pytest.raises(ContextUnavailableError) as raised:
        with factory.open(scope=_scope(1), principal=_principal(1)):
            pytest.fail("failed builder must not yield a source")

    assert str(raised.value) == "CONTEXT_UNAVAILABLE:credentials"
    assert "secret-alpha" not in repr(raised.value)
    assert "secret-alpha" not in repr(factory)
    rendered = "".join(
        traceback.format_exception(
            type(raised.value),
            raised.value,
            raised.value.__traceback__,
        )
    )
    assert "secret-alpha" not in rendered


def test_source_sanitizes_upstream_exception_traceback() -> None:
    secret = "secret-alpha"
    vault = _Vault({"cred_1": secret})

    def builder(**_):
        api = _FailingAPI(secret)
        return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=api)))

    source = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=builder,
    ).open(scope=_scope(1), principal=_principal(1))
    with source, pytest.raises(ContextUnavailableError) as raised:
        source.resolve_thread()

    rendered = "".join(
        traceback.format_exception(
            type(raised.value),
            raised.value,
            raised.value.__traceback__,
        )
    )
    assert secret not in rendered


def test_source_sanitizes_broken_sdk_response_validation() -> None:
    secret = "secret-alpha"
    vault = _Vault({"cred_1": secret})

    class BrokenResponse:
        def success(self):
            try:
                raise RuntimeError(f"broken response exposed {secret}")
            except RuntimeError as exc:
                raise ContextUnavailableError("source") from exc

    class API:
        def get(self, _request):
            return BrokenResponse()

    def builder(**_):
        api = API()
        return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=api)))

    source = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=builder,
    ).open(scope=_scope(1), principal=_principal(1))
    with source, pytest.raises(ContextUnavailableError) as raised:
        source.resolve_thread()
    rendered = "".join(
        traceback.format_exception(
            type(raised.value),
            raised.value,
            raised.value.__traceback__,
        )
    )
    assert raised.value.reason.value == "source"
    assert secret not in rendered
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_source_sanitizes_broken_list_response_validation() -> None:
    secret = "secret-alpha"
    vault = _Vault({"cred_1": secret})

    class Response:
        code = 0
        data = SimpleNamespace(
            items=[
                SimpleNamespace(
                    message_id="om_current_1",
                    chat_id="oc_1",
                    root_id="om_root_1",
                    thread_id="omt_1",
                    deleted=False,
                )
            ]
        )

        def success(self):
            return True

    class BrokenListResponse:
        def success(self):
            raise RuntimeError(f"broken list exposed {secret}")

    class API:
        def get(self, _request):
            return Response()

        def list(self, _request):
            return BrokenListResponse()

    def builder(**_):
        return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=API())))

    source = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=builder,
    ).open(scope=_scope(1), principal=_principal(1))
    with source:
        source.resolve_thread()
        with pytest.raises(ContextUnavailableError) as raised:
            source.list_thread_messages()
    rendered = "".join(
        traceback.format_exception(
            type(raised.value),
            raised.value,
            raised.value.__traceback__,
        )
    )
    assert raised.value.reason.value == "source"
    assert secret not in rendered


def test_factory_close_revokes_active_and_future_employee_leases() -> None:
    vault = _Vault({"cred_1": "secret-alpha"})
    clients = []

    class Client:
        def __init__(self):
            self.close_calls = 0
            self.im = SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace()))

        def close(self):
            self.close_calls += 1

    def builder(**_):
        client = Client()
        clients.append(client)
        return client

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=builder,
    )
    lease = factory.open(scope=_scope(1), principal=_principal(1))
    with lease:
        factory.close()
        with pytest.raises(ContextUnavailableError):
            lease.resolve_thread()
    assert clients[0].close_calls == 1
    assert lease.closed is True
    with pytest.raises(ContextUnavailableError):
        factory.open(scope=_scope(1), principal=_principal(1))


def test_employee_invalidation_revokes_only_target_employee_lease() -> None:
    vault = _Vault({"cred_1": "secret-alpha", "cred_2": "secret-bravo"})

    class Client:
        def __init__(self):
            self.im = SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace()))

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=lambda **_: Client(),
    )
    first = factory.open(scope=_scope(1), principal=_principal(1))
    second = factory.open(scope=_scope(2), principal=_principal(2))
    first.__enter__()
    second.__enter__()

    factory.invalidate_employee("agt_1")

    assert first.closed is True
    assert second.closed is False
    with pytest.raises(ContextUnavailableError):
        factory.open(scope=_scope(1), principal=_principal(1))
    with pytest.raises(ContextUnavailableError):
        first.resolve_thread()
    factory.reactivate_employee("agt_1")
    with factory.open(scope=_scope(1), principal=_principal(1)):
        pass
    second.__exit__(None, None, None)
    factory.close()


def test_production_factory_constructor_cannot_accept_a_prebuilt_client() -> None:
    parameters = inspect.signature(LarkEmployeeMessageSourceFactory).parameters
    assert "client" not in parameters
    assert "client_builder" not in parameters


def test_capability_probe_closes_ephemeral_employee_client() -> None:
    class ApplicationAPI:
        def get(self, request):
            return SimpleNamespace(
                code=0,
                success=lambda: True,
                data=SimpleNamespace(
                    app=SimpleNamespace(app_id=request.app_id)
                ),
            )

    class Client:
        def __init__(self) -> None:
            self.close_calls = 0
            self.application = SimpleNamespace(
                v6=SimpleNamespace(application=ApplicationAPI())
            )

        def close(self) -> None:
            self.close_calls += 1

    client = Client()
    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=_Vault({"cred_1": "secret-alpha"}),
        client_builder=lambda **_: client,
    )

    assert factory.probe(_principal(1)) is True
    assert client.close_calls == 1
    factory.close()


@pytest.mark.parametrize(
    ("code", "success", "returned_app_id"),
    [
        (1, True, "cli_employee_1"),
        (0, False, "cli_employee_1"),
        (0, True, "cli_another_employee"),
    ],
)
def test_capability_probe_rejects_unverified_app_identity_and_closes_client(
    code: int,
    success: bool,
    returned_app_id: str,
) -> None:
    class ApplicationAPI:
        def get(self, _request):
            return SimpleNamespace(
                code=code,
                success=lambda: success,
                data=SimpleNamespace(
                    app=SimpleNamespace(app_id=returned_app_id)
                ),
            )

    class Client:
        def __init__(self) -> None:
            self.close_calls = 0
            self.application = SimpleNamespace(
                v6=SimpleNamespace(application=ApplicationAPI())
            )

        def close(self) -> None:
            self.close_calls += 1

    client = Client()
    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=_Vault({"cred_1": "secret-alpha"}),
        client_builder=lambda **_: client,
    )

    assert factory.probe(_principal(1)) is False
    assert client.close_calls == 1
    factory.close()


def test_factory_close_drains_pending_credential_acquire() -> None:
    resolver_entered = threading.Event()
    release_resolver = threading.Event()
    close_done = threading.Event()
    worker_done = threading.Event()

    class BlockingVault(_Vault):
        def resolve(self, credential_ref, agent_id, app_id):
            resolver_entered.set()
            assert release_resolver.wait(2)
            return super().resolve(credential_ref, agent_id, app_id)

    vault = BlockingVault({"cred_1": "secret-alpha"})
    clients = []

    class Client:
        def __init__(self):
            self.closed = False
            self.im = SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace()))

        def close(self):
            self.closed = True

    def builder(**_):
        client = Client()
        clients.append(client)
        return client

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=vault,
        client_builder=builder,
    )
    lease = factory.open(scope=_scope(1), principal=_principal(1))

    def enter_lease():
        try:
            lease.__enter__()
        except ContextUnavailableError:
            pass
        finally:
            worker_done.set()

    worker = threading.Thread(target=enter_lease)
    worker.start()
    assert resolver_entered.wait(2)
    closer = threading.Thread(target=lambda: (factory.close(), close_done.set()))
    closer.start()
    assert not close_done.wait(0.05)
    release_resolver.set()
    assert worker_done.wait(2)
    assert close_done.wait(2)
    worker.join()
    closer.join()
    assert len(clients) == 1
    assert clients[0].closed is True
    assert lease.closed is True


def test_employee_invalidation_drains_inflight_capability_probe() -> None:
    resolver_entered = threading.Event()
    release_resolver = threading.Event()
    invalidated = threading.Event()
    probe_results: list[bool] = []

    class BlockingVault(_Vault):
        def resolve(self, credential_ref, agent_id, app_id):
            resolver_entered.set()
            assert release_resolver.wait(2)
            return super().resolve(credential_ref, agent_id, app_id)

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=BlockingVault({"cred_1": "secret-alpha"}),
        client_builder=lambda **_: object(),
    )
    probe = threading.Thread(
        target=lambda: probe_results.append(factory.probe(_principal(1)))
    )
    probe.start()
    assert resolver_entered.wait(2)
    invalidator = threading.Thread(
        target=lambda: (
            factory.invalidate_employee("agt_1"),
            invalidated.set(),
        )
    )
    invalidator.start()
    assert not invalidated.wait(0.05)

    release_resolver.set()
    assert invalidated.wait(2)
    probe.join()
    invalidator.join()
    assert probe_results == [False]


def test_employee_invalidation_does_not_wait_for_another_employee_probe() -> None:
    resolver_entered = threading.Event()
    release_resolver = threading.Event()
    invalidated = threading.Event()

    class BlockingVault(_Vault):
        def resolve(self, credential_ref, agent_id, app_id):
            resolver_entered.set()
            assert release_resolver.wait(2)
            return super().resolve(credential_ref, agent_id, app_id)

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=BlockingVault({"cred_2": "secret-bravo"}),
        client_builder=lambda **_: object(),
    )
    probe = threading.Thread(target=lambda: factory.probe(_principal(2)))
    probe.start()
    assert resolver_entered.wait(2)

    invalidator = threading.Thread(
        target=lambda: (
            factory.invalidate_employee("agt_1"),
            invalidated.set(),
        )
    )
    invalidator.start()
    assert invalidated.wait(0.5)

    release_resolver.set()
    probe.join()
    invalidator.join()
    factory.close()


def test_factory_close_revokes_during_response_validation_commit_window() -> None:
    get_entered = threading.Event()
    release_get = threading.Event()
    close_done = threading.Event()
    errors = []

    class API:
        def get(self, _request):
            class Response:
                code = 0
                data = SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            message_id="om_current_1",
                            chat_id="oc_1",
                            root_id="om_root_1",
                            thread_id="omt_1",
                            deleted=False,
                        )
                    ]
                )

                def success(self):
                    get_entered.set()
                    assert release_get.wait(2)
                    return True

            return Response()

    def builder(**_):
        return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=API())))

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=_Vault({"cred_1": "secret-alpha"}),
        client_builder=builder,
    )
    lease = factory.open(scope=_scope(1), principal=_principal(1))
    lease.__enter__()

    def resolve():
        try:
            lease.resolve_thread()
        except ContextUnavailableError as exc:
            errors.append(exc.reason)

    worker = threading.Thread(target=resolve)
    worker.start()
    assert get_entered.wait(2)
    closer = threading.Thread(target=lambda: (factory.close(), close_done.set()))
    closer.start()
    assert not close_done.wait(0.05)
    release_get.set()
    worker.join(2)
    assert close_done.wait(2)
    closer.join()
    assert errors and errors[0].value == "source"
    assert lease.closed is True


def test_one_lease_cannot_be_entered_concurrently_twice() -> None:
    builds = []

    class Client:
        def __init__(self):
            self.close_calls = 0
            self.im = SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace()))

        def close(self):
            self.close_calls += 1

    def builder(**_):
        client = Client()
        builds.append(client)
        return client

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=_Vault({"cred_1": "secret-alpha"}),
        client_builder=builder,
    )
    lease = factory.open(scope=_scope(1), principal=_principal(1))
    barrier = threading.Barrier(3)
    results = []

    def enter():
        barrier.wait()
        try:
            lease.__enter__()
            results.append("entered")
        except ContextUnavailableError:
            results.append("rejected")

    workers = [threading.Thread(target=enter) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(2)
    lease.close()
    factory.close()
    assert sorted(results) == ["entered", "rejected"]
    assert len(builds) == 1
    assert builds[0].close_calls == 1


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan")])
def test_factory_requires_a_positive_finite_request_timeout(timeout) -> None:
    with pytest.raises(ValueError):
        LarkEmployeeMessageSourceFactory(
            credential_resolver=_Vault({}),
            request_timeout_seconds=timeout,
        )
