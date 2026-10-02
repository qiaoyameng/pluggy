"""Tests for the optional implementation-level observer.

The observer complements ``add_hookcall_monitoring`` by reporting every hook
implementation that actually runs (plain implementations and the entry/exit
phases of wrappers), with call ids linking nested calls, injectable clocks and
fail-safe error handling.
"""

from __future__ import annotations

from typing import Any

import pytest

import pluggy
from pluggy import HookImplObserver
from pluggy import PHASE_IMPL
from pluggy import PHASE_WRAPPER_ENTER
from pluggy import PHASE_WRAPPER_EXIT
from pluggy import PluggyObserverWarning
from pluggy._callers import _multicall
from pluggy._hooks import HookImpl


hookspec = pluggy.HookspecMarker("example")
hookimpl = pluggy.HookimplMarker("example")


class FixedClock:
    """Deterministic, strictly increasing clock."""

    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> float:
        current = self.value
        self.value += 1
        return current


class Recorder(HookImplObserver):
    """Record every callback as a short, easy-to-compare tuple."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.errors: list[tuple[str, BaseException]] = []

    def on_hook_call(self, event: Any) -> None:
        self.events.append(
            ("call", event.call_id, event.hook_name, event.parent_call_id)
        )

    def on_hook_call_result(self, event: Any) -> None:
        self.events.append(
            (
                "call_result",
                event.call_id,
                event.hook_name,
                event.start_time,
                event.end_time,
                None if event.exception is None else type(event.exception),
            )
        )

    def on_impl_start(self, event: Any) -> None:
        self.events.append(
            (
                "start",
                event.call_id,
                event.impl.plugin_name,
                event.phase,
                event.start_time,
            )
        )

    def on_impl_end(self, event: Any) -> None:
        self.events.append(
            (
                "end",
                event.call_id,
                event.impl.plugin_name,
                event.phase,
                event.start_time,
                event.end_time,
                event.result,
            )
        )

    def on_impl_exception(self, event: Any) -> None:
        self.events.append(
            (
                "exception",
                event.call_id,
                event.impl.plugin_name,
                event.phase,
                event.start_time,
                event.end_time,
                type(event.exception),
            )
        )

    def on_observer_error(self, callback_name: str, exc: BaseException) -> None:
        self.errors.append((callback_name, exc))


@pytest.fixture
def pm() -> pluggy.PluginManager:
    manager = pluggy.PluginManager("example")

    class Spec:
        @hookspec
        def outer(self) -> object: ...

        @hookspec
        def inner(self) -> object: ...

        @hookspec(firstresult=True)
        def first(self) -> object: ...

        @hookspec
        def failing(self) -> object: ...

    manager.add_hookspecs(Spec)
    return manager


def test_observer_is_optional_with_zero_events(pm: pluggy.PluginManager) -> None:
    class P:
        @hookimpl
        def outer(self):
            return 1

    pm.register(P(), "p")

    # No observer installed: calls behave normally and allocate no events.
    assert pm.hook.outer() == [1]


def test_fixed_clock_two_wrappers_and_nested_call(
    pm: pluggy.PluginManager,
) -> None:
    """The headline sequence: two wrappers and a nested hook call.

    Ordering: wrappers enter outside-in (tryfirst outermost), the plain impl
    runs and triggers a nested call, then wrappers exit inside-out. The nested
    call records the outer call id as its parent.
    """

    class Wa:
        @hookimpl(wrapper=True, tryfirst=True)
        def outer(self):
            value = yield
            return ("wa", value)

    class Wb:
        @hookimpl(wrapper=True)
        def outer(self):
            value = yield
            return ("wb", value)

    class Plain:
        @hookimpl
        def outer(self):
            # ``inner`` is a non-firstresult hook, so unwrap its list.
            return pm.hook.inner()[0]

    class Nested:
        @hookimpl
        def inner(self):
            return 42

    pm.register(Wa(), "wa")
    pm.register(Wb(), "wb")
    pm.register(Plain(), "plain")
    pm.register(Nested(), "nested")

    clock = FixedClock()
    observer = Recorder()
    pm.add_hookimpl_observer(observer, clock=clock)

    result = pm.hook.outer()
    assert result == ("wa", ("wb", [42]))

    outer_id, inner_id = 1, 2
    assert observer.events == [
        ("call", outer_id, "outer", None),
        ("start", outer_id, "wa", PHASE_WRAPPER_ENTER, 1),
        ("end", outer_id, "wa", PHASE_WRAPPER_ENTER, 1, 2, None),
        ("start", outer_id, "wb", PHASE_WRAPPER_ENTER, 3),
        ("end", outer_id, "wb", PHASE_WRAPPER_ENTER, 3, 4, None),
        ("start", outer_id, "plain", PHASE_IMPL, 5),
        # The nested hook call starts inside the plain implementation and
        # links back to the outer call.
        ("call", inner_id, "inner", outer_id),
        ("start", inner_id, "nested", PHASE_IMPL, 7),
        ("end", inner_id, "nested", PHASE_IMPL, 7, 8, 42),
        ("call_result", inner_id, "inner", 6, 9, None),
        ("end", outer_id, "plain", PHASE_IMPL, 5, 10, 42),
        ("start", outer_id, "wb", PHASE_WRAPPER_EXIT, 11),
        ("end", outer_id, "wb", PHASE_WRAPPER_EXIT, 11, 12, ("wb", [42])),
        ("start", outer_id, "wa", PHASE_WRAPPER_EXIT, 13),
        (
            "end",
            outer_id,
            "wa",
            PHASE_WRAPPER_EXIT,
            13,
            14,
            ("wa", ("wb", [42])),
        ),
        ("call_result", outer_id, "outer", 0, 15, None),
    ]
    assert observer.errors == []


def test_firstresult_unexecuted_plugins_are_not_reported(
    pm: pluggy.PluginManager,
) -> None:
    class A:
        @hookimpl(tryfirst=True)
        def first(self):
            return "a"

    class B:
        @hookimpl
        def first(self):
            return "b"

    class C:
        @hookimpl
        def first(self):
            return "c"

    pm.register(A(), "a")
    pm.register(B(), "b")
    pm.register(C(), "c")

    observer = Recorder()
    pm.add_hookimpl_observer(observer, clock=FixedClock())

    assert pm.hook.first() == "a"

    impls = [event for event in observer.events if event[0] in {"start", "end"}]
    # Only ``a`` started and finished; ``b`` and ``c`` never ran and thus
    # must not be reported as successfully executed.
    names = {event[2] for event in impls}
    assert names == {"a"}
    assert not any(event[0] == "exception" for event in observer.events)


def test_impl_exception_keeps_business_result(pm: pluggy.PluginManager) -> None:
    class P:
        @hookimpl
        def failing(self):
            raise ValueError("boom")

    pm.register(P(), "p")

    observer = Recorder()
    pm.add_hookimpl_observer(observer, clock=FixedClock())

    with pytest.raises(ValueError, match="boom"):
        pm.hook.failing()

    call_id = observer.events[0][1]
    assert [
        event for event in observer.events if event[0] == "exception"
    ] == [
        ("exception", call_id, "p", PHASE_IMPL, 1, 2, ValueError),
    ]
    call_result = [event for event in observer.events if event[0] == "call_result"]
    assert call_result == [
        ("call_result", call_id, "failing", 0, 3, ValueError)
    ]


def test_wrapper_exit_sees_exception_through_to_handling(
    pm: pluggy.PluginManager,
) -> None:
    class Wa:
        @hookimpl(wrapper=True, tryfirst=True)
        def failing(self):
            try:
                yield
            except ValueError:
                return "handled"

    class P:
        @hookimpl
        def failing(self):
            raise ValueError("boom")

    pm.register(Wa(), "wa")
    pm.register(P(), "p")

    observer = Recorder()
    pm.add_hookimpl_observer(observer, clock=FixedClock())

    # The wrapper swallows the plugin exception; the business result stands.
    assert pm.hook.failing() == "handled"

    phases = [
        (event[2], event[3], event[0]) for event in observer.events
    ]
    assert ("p", PHASE_IMPL, "exception") in phases
    assert ("wa", PHASE_WRAPPER_EXIT, "end") in phases
    call_id = observer.events[0][1]
    assert observer.events[-1] == (
        "call_result",
        call_id,
        "failing",
        0,
        7,
        None,
    )


def test_observer_raising_does_not_change_business_result(
    pm: pluggy.PluginManager,
) -> None:
    class Broken(HookImplObserver):
        def __init__(self) -> None:
            self.failed_callbacks: list[str] = []

        def on_impl_end(self, event: Any) -> None:
            self.failed_callbacks.append("on_impl_end")
            raise RuntimeError("observer exploded")

    class Wa:
        @hookimpl(wrapper=True, tryfirst=True)
        def outer(self):
            value = yield
            return ("wa", value)

    class P:
        @hookimpl
        def outer(self):
            return 5

    pm.register(Wa(), "wa")
    pm.register(P(), "p")

    observer = Broken()
    pm.add_hookimpl_observer(observer, clock=FixedClock())

    import warnings

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        result = pm.hook.outer()

    # The plugin result is unchanged despite the observer failing.
    assert result == ("wa", [5])
    assert observer.failed_callbacks  # plain impl end and wrapper exit end
    assert set(observer.failed_callbacks) == {"on_impl_end"}
    observer_warnings = [
        warning
        for warning in recorded
        if issubclass(warning.category, PluggyObserverWarning)
    ]
    # One diagnostic per failing callback (plain impl end + wrapper exits).
    assert observer_warnings
    assert all(
        "on_impl_end" in str(warning.message)
        and "observer exploded" in str(warning.message)
        for warning in observer_warnings
    )


def test_observer_raising_on_exception_does_not_mask_plugin_error(
    pm: pluggy.PluginManager,
) -> None:
    class Broken(HookImplObserver):
        def on_impl_exception(self, event: Any) -> None:
            raise RuntimeError("observer exploded")

    class P:
        @hookimpl
        def failing(self):
            raise ValueError("plugin error")

    pm.register(P(), "p")
    pm.add_hookimpl_observer(Broken(), clock=FixedClock())

    # The plugin's own exception is the one that propagates, never the
    # observer's RuntimeError.
    with pytest.raises(ValueError, match="plugin error"):
        pm.hook.failing()


def test_custom_error_channel_receives_observer_failures(
    pm: pluggy.PluginManager,
) -> None:
    seen: list[tuple[str, BaseException]] = []

    class Channeled(HookImplObserver):
        def on_impl_end(self, event: Any) -> None:
            raise KeyError("nope")

        def on_observer_error(self, callback_name: str, exc: BaseException) -> None:
            seen.append((callback_name, exc))

    class P:
        @hookimpl
        def outer(self):
            return 1

    pm.register(P(), "p")
    import warnings

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        pm.add_hookimpl_observer(Channeled(), clock=FixedClock())
        assert pm.hook.outer() == [1]

    assert [name for name, _ in seen] == ["on_impl_end"]
    assert isinstance(seen[0][1], KeyError)
    # The default warning channel was replaced, so no warning was emitted.
    assert not [w for w in record if issubclass(w.category, PluggyObserverWarning)]


def test_undo_stops_observation(pm: pluggy.PluginManager) -> None:
    class P:
        @hookimpl
        def outer(self):
            return 1

    pm.register(P(), "p")

    observer = Recorder()
    undo = pm.add_hookimpl_observer(observer, clock=FixedClock())

    assert pm.hook.outer() == [1]
    assert len(observer.events) > 0

    undo()
    observer.events.clear()
    assert pm.hook.outer() == [1]
    assert observer.events == []


def test_multiple_independent_calls_get_distinct_ids(
    pm: pluggy.PluginManager,
) -> None:
    class P:
        @hookimpl
        def outer(self):
            return 1

    pm.register(P(), "p")
    observer = Recorder()
    pm.add_hookimpl_observer(observer, clock=FixedClock())

    pm.hook.outer()
    pm.hook.outer()

    calls = [event for event in observer.events if event[0] == "call"]
    assert len(calls) == 2
    assert calls[0][1] != calls[1][1]
    assert all(call[3] is None for call in calls)


def test_nested_call_without_outer_observer_still_links(
    pm: pluggy.PluginManager,
) -> None:
    """A nested observed call made from an unobserved context has no parent."""

    class P:
        @hookimpl
        def outer(self):
            return pm.hook.inner()

    class N:
        @hookimpl
        def inner(self):
            return 3

    pm.register(P(), "p")
    pm.register(N(), "n")
    observer = Recorder()
    pm.add_hookimpl_observer(observer, clock=FixedClock())

    assert pm.hook.outer() == [[3]]
    call_events = [event for event in observer.events if event[0] == "call"]
    # outer and inner both observed, inner parented by outer.
    assert call_events[0][2] == "outer"
    assert call_events[1][2] == "inner"
    assert call_events[1][3] == call_events[0][1]


def test_old_style_hookwrapper_phases(pm: pluggy.PluginManager) -> None:
    class HW:
        @hookimpl(hookwrapper=True)
        def outer(self):
            yield

    class P:
        @hookimpl
        def outer(self):
            return 9

    pm.register(HW(), "hw")
    pm.register(P(), "p")
    observer = Recorder()
    pm.add_hookimpl_observer(observer, clock=FixedClock())

    assert pm.hook.outer() == [9]

    phase_events = [
        (event[2], event[3], event[0])
        for event in observer.events
        if event[0] in {"start", "end"}
    ]
    assert phase_events == [
        ("hw", PHASE_WRAPPER_ENTER, "start"),
        ("hw", PHASE_WRAPPER_ENTER, "end"),
        ("p", PHASE_IMPL, "start"),
        ("p", PHASE_IMPL, "end"),
        ("hw", PHASE_WRAPPER_EXIT, "start"),
        ("hw", PHASE_WRAPPER_EXIT, "end"),
    ]


def test_direct_multicall_accepts_observers() -> None:
    observer = Recorder()

    @hookimpl
    def f():
        return 11

    impl = HookImpl(None, "<temp>", f, f.example_impl)  # type: ignore[attr-defined]
    result = _multicall(
        "foo",
        [impl],
        {},
        False,
        [(observer, FixedClock())],
    )
    assert result == [11]
    assert any(event[0] == "start" for event in observer.events)


def test_observer_works_with_hookcall_monitoring_and_tracing(
    pm: pluggy.PluginManager,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Observers cooperate with wrappers of ``_inner_hookexec``."""

    class P:
        @hookimpl
        def outer(self):
            return 1

    pm.register(P(), "p")

    monitored: list[str] = []

    def before(hook_name, hook_impls, kwargs):  # type: ignore[no-untyped-def]
        monitored.append(f"before:{hook_name}")

    def after(outcome, hook_name, hook_impls, kwargs):  # type: ignore[no-untyped-def]
        monitored.append(f"after:{hook_name}")

    undo_monitoring = pm.add_hookcall_monitoring(before, after)
    pm.trace.root.setwriter(lambda text: capsys.readouterr())  # keep it quiet
    undo_tracing = pm.enable_tracing()
    observer = Recorder()
    undo_observer = pm.add_hookimpl_observer(observer, clock=FixedClock())

    assert pm.hook.outer() == [1]
    assert monitored == ["before:outer", "after:outer"]
    assert any(event[0] == "start" for event in observer.events)

    # Removing the wrappers leaves the observer working, and removing the
    # observer leaves the wrappers working.
    undo_tracing()
    undo_monitoring()
    observer.events.clear()
    assert pm.hook.outer() == [1]
    assert any(event[0] == "start" for event in observer.events)

    undo_observer()
    observer.events.clear()
    assert pm.hook.outer() == [1]
    assert observer.events == []


def test_broken_clock_does_not_break_business_result(
    pm: pluggy.PluginManager,
) -> None:
    def broken_clock() -> float:
        raise RuntimeError("clock unavailable")

    class P:
        @hookimpl
        def outer(self):
            return 8

    pm.register(P(), "p")
    observer = Recorder()
    pm.add_hookimpl_observer(observer, clock=broken_clock)

    import math

    assert pm.hook.outer() == [8]
    assert observer.errors
    assert all(isinstance(exc, RuntimeError) for _, exc in observer.errors)
    end_events = [event for event in observer.events if event[0] == "end"]
    assert end_events
    assert all(math.isnan(event[5]) for event in end_events)


def test_observer_raising_on_start_does_not_stop_plugins(
    pm: pluggy.PluginManager,
) -> None:
    class FailStart(Recorder):
        def on_impl_start(self, event: Any) -> None:
            super().on_impl_start(event)
            raise IndexError("start boom")

    class P:
        @hookimpl
        def outer(self):
            return 6

    pm.register(P(), "p")
    observer = FailStart()
    pm.add_hookimpl_observer(observer, clock=FixedClock())

    # The implementation still runs even though its start callback raised.
    assert pm.hook.outer() == [6]
    assert {name for name, _ in observer.errors} == {"on_impl_start"}
