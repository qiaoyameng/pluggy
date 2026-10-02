"""Tests for :ref:`asynchronous hooks <async>`.

Asynchronous hooks are declared with ``async_=True`` on the specification and
awaited through :meth:`pluggy.HookCaller.acall`. Both synchronous and
coroutine implementations (and both synchronous and asynchronous wrappers)
take part in the same, serially executed call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
import types
from typing import Any

import pytest

from pluggy import HookCallError
from pluggy import HookimplMarker
from pluggy import HookspecMarker
from pluggy import PluginManager
from pluggy import PluginValidationError
from pluggy import Result


hookspec = HookspecMarker("example")
hookimpl = HookimplMarker("example")


@pytest.fixture
def pm() -> PluginManager:
    return PluginManager("example")


class AsyncSpec:
    @hookspec(async_=True)
    async def async_hook(self, arg: int) -> int: ...


class AsyncFirstResultSpec:
    @hookspec(firstresult=True, async_=True)
    async def async_hook(self, arg: int) -> int | None: ...


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_is_async_detection(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncSpec)
    assert pm.hook.async_hook.is_async()

    class Plugin:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            return arg

    pm.register(Plugin())
    assert pm.hook.async_hook.is_async()


def test_async_spec_with_only_sync_impls(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncSpec)

    class Plugin:
        @hookimpl
        def async_hook(self, arg: int) -> int:
            return arg + 1

    pm.register(Plugin())
    assert run(pm.hook.async_hook.acall(arg=1)) == [2]


def test_coroutine_implementations_run_serially(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncSpec)
    log: list[str] = []
    gate = asyncio.Event()

    class First:
        @hookimpl(tryfirst=True)
        async def async_hook(self, arg: int) -> int:
            log.append("first-start")
            await gate.wait()
            log.append("first-end")
            return arg

    class Second:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            log.append("second-start")
            # Must not start before the first implementation finished.
            assert log == ["first-start", "first-end", "second-start"]
            log.append("second-end")
            return arg + 1

    pm.register(First())
    pm.register(Second())

    async def scenario() -> list[int]:
        task = asyncio.ensure_future(pm.hook.async_hook.acall(arg=1))
        await asyncio.sleep(0)
        assert log == ["first-start"]
        gate.set()
        return await task

    assert run(scenario()) == [1, 2]
    assert log == ["first-start", "first-end", "second-start", "second-end"]


def test_sync_and_async_implementations(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncSpec)

    class Sync:
        @hookimpl
        def async_hook(self, arg: int) -> int:
            return arg * 10

    class Asyn:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            await asyncio.sleep(0)
            return arg * 100

    pm.register(Sync())
    pm.register(Asyn())
    assert sorted(run(pm.hook.async_hook.acall(arg=2))) == [20, 200]


def test_firstresult(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncFirstResultSpec)

    class NoneResult:
        @hookimpl(tryfirst=True)
        async def async_hook(self, arg: int) -> None:
            return None

    class Hit:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            return arg + 1

    class After:
        @hookimpl(trylast=True)
        async def async_hook(self, arg: int) -> int:
            raise AssertionError("should not be reached after a first result")

    pm.register(NoneResult())
    pm.register(Hit())
    pm.register(After())
    assert run(pm.hook.async_hook.acall(arg=1)) == 2


def test_sync_call_rejects_async_spec(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncSpec)

    class Plugin:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            return arg

    pm.register(Plugin())
    with pytest.raises(HookCallError):
        pm.hook.async_hook(arg=1)  # type: ignore[operator]


def test_sync_call_rejects_async_impl_without_spec(pm: PluginManager) -> None:
    class Plugin:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            return arg

    pm.register(Plugin())
    with pytest.raises(HookCallError):
        pm.hook.async_hook(arg=1)  # type: ignore[operator]


def test_async_impl_rejected_for_sync_spec(pm: PluginManager) -> None:
    class SyncSpec:
        @hookspec
        def sync_hook(self, arg: int) -> int: ...

    pm.add_hookspecs(SyncSpec)

    class Plugin:
        @hookimpl
        async def sync_hook(self, arg: int) -> int:
            return arg

    with pytest.raises(PluginValidationError):
        pm.register(Plugin())


def test_async_wrapper_rejected_for_sync_spec(pm: PluginManager) -> None:
    class SyncSpec:
        @hookspec
        def sync_hook(self) -> None: ...

    pm.add_hookspecs(SyncSpec)

    class Plugin:
        @hookimpl(wrapper=True)
        async def sync_hook(self) -> Iterator[None]:
            yield  # pragma: no cover

    with pytest.raises(PluginValidationError):
        pm.register(Plugin())


def test_sync_wrapper_in_async_hook_nesting(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncSpec)
    log: list[str] = []

    class AsyncInner:
        @hookimpl(tryfirst=True, wrapper=True)
        async def async_hook(self, arg: int):
            log.append("async-inner-start")
            result = yield
            log.append(f"async-inner-end:{result}")

    class SyncOuter:
        @hookimpl(wrapper=True)
        def async_hook(self, arg: int):
            log.append("sync-outer-start")
            result = yield
            log.append(f"sync-outer-end:{result}")
            return result

    class Impl:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            log.append("impl")
            return arg

    pm.register(AsyncInner())
    pm.register(SyncOuter())
    pm.register(Impl())
    assert run(pm.hook.async_hook.acall(arg=3)) == [3]
    assert log == [
        "async-inner-start",
        "sync-outer-start",
        "impl",
        "sync-outer-end:[3]",
        "async-inner-end:[3]",
    ]


def test_async_wrapper_nested_order(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncSpec)
    log: list[Any] = []

    class Inner:
        @hookimpl(tryfirst=True, wrapper=True)
        async def async_hook(self, arg: int):
            log.append("inner-start")
            result = yield
            log.append(("inner", result))

    class Outer:
        @hookimpl(wrapper=True)
        async def async_hook(self, arg: int):
            log.append("outer-start")
            result = yield
            log.append(("outer", result))

    class Impl:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            log.append("impl")
            return arg

    pm.register(Inner())
    pm.register(Outer())
    pm.register(Impl())
    assert run(pm.hook.async_hook.acall(arg=5)) == [5]
    assert log == [
        "inner-start",
        "outer-start",
        "impl",
        ("outer", [5]),
        ("inner", [5]),
    ]


def test_async_wrapper_observes_exception_and_repropagates(
    pm: PluginManager,
) -> None:
    class Spec:
        @hookspec(async_=True)
        async def boom(self) -> None: ...

    pm.add_hookspecs(Spec)
    seen: dict[str, BaseException] = {}

    class Wrap:
        @hookimpl(wrapper=True)
        async def boom(self):
            try:
                yield
            except ValueError as exc:
                seen["exc"] = exc
                raise

    class Impl:
        @hookimpl
        async def boom(self) -> None:
            raise ValueError("boom")

    pm.register(Wrap())
    pm.register(Impl())
    with pytest.raises(ValueError, match="boom"):
        run(pm.hook.boom.acall())
    assert isinstance(seen.get("exc"), ValueError)


def test_oldstyle_async_hookwrapper_result_replacement(
    pm: PluginManager,
) -> None:
    pm.add_hookspecs(AsyncFirstResultSpec)

    class Wrap:
        @hookimpl(hookwrapper=True)
        async def async_hook(self, arg: int):
            outcome: Result[int] = yield
            outcome.force_result(arg * 2)

    class Impl:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            return arg

    pm.register(Wrap())
    pm.register(Impl())
    assert run(pm.hook.async_hook.acall(arg=21)) == 42


def test_oldstyle_async_hookwrapper_forces_exception(
    pm: PluginManager,
) -> None:
    class Spec:
        @hookspec(async_=True)
        async def hook(self) -> None: ...

    pm.add_hookspecs(Spec)

    class Wrap:
        @hookimpl(hookwrapper=True)
        async def hook(self):
            outcome: Result[None] = yield
            outcome.force_exception(RuntimeError("forced"))

    class Impl:
        @hookimpl
        async def hook(self) -> None:
            return None

    pm.register(Wrap())
    pm.register(Impl())
    with pytest.raises(RuntimeError, match="forced"):
        run(pm.hook.hook.acall())


def test_oldstyle_async_hookwrapper_sees_exception(pm: PluginManager) -> None:
    class Spec:
        @hookspec(async_=True)
        async def hook(self) -> None: ...

    pm.add_hookspecs(Spec)
    seen: dict[str, BaseException] = {}

    class Wrap:
        @hookimpl(hookwrapper=True)
        async def hook(self):
            outcome: Result[None] = yield
            seen["exc"] = outcome.exception  # type: ignore[assignment]
            # Swallow the inner exception.
            outcome.force_result(None)

    class Impl:
        @hookimpl
        async def hook(self) -> None:
            raise ValueError("inner")

    pm.register(Wrap())
    pm.register(Impl())
    assert run(pm.hook.hook.acall()) is None
    assert isinstance(seen.get("exc"), ValueError)


def test_cancellation_unwinds_entered_wrappers(pm: PluginManager) -> None:
    """Cancel the call while an implementation is paused. Every wrapper that
    was already entered must run its post-yield (``finally``) block before the
    cancelled call completes."""
    pm.add_hookspecs(AsyncSpec)
    entered: list[str] = []
    finished: list[str] = []
    paused = asyncio.Event()
    release = asyncio.Event()

    class Outer:
        @hookimpl(wrapper=True)
        async def async_hook(self, arg: int):
            entered.append("outer")
            try:
                yield
            finally:
                finished.append("outer")

    class Inner:
        @hookimpl(tryfirst=True, wrapper=True)
        async def async_hook(self, arg: int):
            entered.append("inner")
            try:
                yield
            finally:
                finished.append("inner")

    class Paused:
        @hookimpl(tryfirst=True)
        async def async_hook(self, arg: int) -> int:
            entered.append("impl")
            paused.set()
            await release.wait()
            return arg

    class Never:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            raise AssertionError("must not start while the first impl is paused")

    pm.register(Outer())
    pm.register(Inner())
    pm.register(Paused())
    pm.register(Never())

    async def scenario() -> None:
        task = asyncio.ensure_future(pm.hook.async_hook.acall(arg=1))
        await paused.wait()
        assert entered == ["inner", "outer", "impl"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Wrappers unwind in nested order.
        assert finished == ["outer", "inner"]

    run(scenario())


def test_async_kwargs_binding(pm: PluginManager) -> None:
    class Spec:
        @hookspec(async_=True)
        async def hook(self, x: int, y: int) -> int: ...

    pm.add_hookspecs(Spec)

    class OnlyX:
        @hookimpl
        async def hook(self, x: int) -> int:
            return x

    class OnlyY:
        @hookimpl
        async def hook(self, y: int) -> int:
            return y

    pm.register(OnlyX())
    pm.register(OnlyY())
    assert sorted(run(pm.hook.hook.acall(x=1, y=2))) == [1, 2]

    with pytest.warns(UserWarning), pytest.raises(HookCallError):
        run(pm.hook.hook.acall(x=1))


def test_acall_extra(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncSpec)

    class Registered:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            return arg

    pm.register(Registered())

    async def extra(arg: int) -> int:
        return arg + 100

    assert run(pm.hook.async_hook.acall_extra([extra], {"arg": 1})) == [101, 1]


def test_async_tracing(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncSpec)
    events: list[str] = []

    class Plugin:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            return arg + 1

    pm.register(Plugin())
    pm.add_hookcall_monitoring(
        lambda *a: events.append("before"),
        lambda outcome, *a: events.append(
            f"after:{outcome.get_result()}"
        ),
    )
    assert run(pm.hook.async_hook.acall(arg=1)) == [2]
    assert events == ["before", "after:[2]"]


def test_sync_wrapper_return_value_replaces_in_async_hook(
    pm: PluginManager,
) -> None:
    pm.add_hookspecs(AsyncFirstResultSpec)

    class Wrap:
        @hookimpl(wrapper=True)
        def async_hook(self, arg: int):
            value = yield
            return ("wrapped", value)

    class Impl:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            return arg

    pm.register(Wrap())
    pm.register(Impl())
    assert run(pm.hook.async_hook.acall(arg=1)) == ("wrapped", 1)


def test_sync_oldstyle_hookwrapper_in_async_hook(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncFirstResultSpec)

    class Wrap:
        @hookimpl(hookwrapper=True)
        def async_hook(self, arg: int):
            outcome: Result[int] = yield
            outcome.force_result("forced")

    class Impl:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            return arg

    pm.register(Wrap())
    pm.register(Impl())
    assert run(pm.hook.async_hook.acall(arg=1)) == "forced"


def test_async_wrapper_teardown_raise_replaces_outcome(
    pm: PluginManager,
) -> None:
    class Spec:
        @hookspec(async_=True)
        async def hook(self) -> int: ...

    pm.add_hookspecs(Spec)

    class Wrap:
        @hookimpl(wrapper=True)
        async def hook(self):
            yield
            raise RuntimeError("teardown")

    class Impl:
        @hookimpl
        async def hook(self) -> int:
            return 1

    pm.register(Wrap())
    pm.register(Impl())
    with pytest.raises(RuntimeError, match="teardown"):
        run(pm.hook.hook.acall())


def test_mixed_wrapper_exception_chain(pm: PluginManager) -> None:
    class Spec:
        @hookspec(async_=True)
        async def hook(self) -> None: ...

    pm.add_hookspecs(Spec)
    order: list[str] = []

    class AsyncWrap:
        @hookimpl(tryfirst=True, wrapper=True)
        async def hook(self):
            order.append("async-in")
            try:
                yield
            except ValueError:
                order.append("async-catch")
                raise
            finally:
                order.append("async-fin")

    class SyncWrap:
        @hookimpl(wrapper=True)
        def hook(self):
            order.append("sync-in")
            try:
                yield
            except ValueError:
                order.append("sync-catch")
                raise
            finally:
                order.append("sync-fin")

    class Impl:
        @hookimpl
        async def hook(self) -> None:
            raise ValueError("boom")

    pm.register(AsyncWrap())
    pm.register(SyncWrap())
    pm.register(Impl())
    with pytest.raises(ValueError, match="boom"):
        run(pm.hook.hook.acall())
    assert order == [
        "async-in",
        "sync-in",
        "sync-catch",
        "sync-fin",
        "async-catch",
        "async-fin",
    ]


def test_acall_is_a_coroutine_without_running_a_loop(pm: PluginManager) -> None:
    pm.add_hookspecs(AsyncSpec)

    class Plugin:
        @hookimpl
        async def async_hook(self, arg: int) -> int:
            return arg

    pm.register(Plugin())
    coro = pm.hook.async_hook.acall(arg=1)
    assert isinstance(coro, types.CoroutineType)
    coro.close()


def test_historic_async_spec_rejected(pm: PluginManager) -> None:
    with pytest.raises(ValueError):

        class Spec:
            @hookspec(historic=True, async_=True)
            async def hook(self) -> None: ...


def test_historic_spec_rejects_coroutine_impl(pm: PluginManager) -> None:
    class Spec:
        @hookspec(historic=True)
        def hook(self, arg: int) -> int: ...

    pm.add_hookspecs(Spec)

    class Plugin:
        @hookimpl
        async def hook(self, arg: int) -> int:
            return arg

    with pytest.raises(PluginValidationError):
        pm.register(Plugin())
