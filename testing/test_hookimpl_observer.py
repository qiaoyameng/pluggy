import logging

import pytest

from pluggy import HookImplEvent
from pluggy import HookimplMarker
from pluggy import HookspecMarker
from pluggy import PluginManager


hookspec = HookspecMarker("example")
hookimpl = HookimplMarker("example")


class FixedClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


def test_observer_reports_wrappers_and_nested_call() -> None:
    pm = PluginManager("example")

    class Hooks:
        @hookspec
        def outer(self):
            pass

        @hookspec(firstresult=True)
        def inner(self):
            pass

    pm.add_hookspecs(Hooks)
    events: list[HookImplEvent] = []
    undo = pm.add_hookimpl_observer(events.append, time_source=FixedClock())

    class Plugin:
        @hookimpl(specname="inner")
        def inner_impl(self):
            return "inner result"

        @hookimpl(specname="outer")
        def outer_impl(self):
            return pm.hook.inner()

        @hookimpl(wrapper=True, trylast=True, specname="outer")
        def inner_outer_wrapper(self):
            result = yield
            return f"inner wrapper {result[0]}"

        @hookimpl(wrapper=True, tryfirst=True, specname="outer")
        def outer_outer_wrapper(self):
            result = yield
            return f"outer wrapper {result}"

    pm.register(Plugin(), name="plugin")

    assert pm.hook.outer() == "outer wrapper inner wrapper inner result"

    actual = [
        (
            event.event,
            event.plugin_name,
            event.timestamp,
            event.elapsed,
            event.result,
            event.exception,
            event.parent_call_id,
        )
        for event in events
    ]
    outer_call = events[0].call_id
    inner_call = next(
        event.call_id
        for event in events
        if event.hook_impl.function.__name__ == "inner_impl"
    )

    assert outer_call != inner_call
    assert actual == [
        ("wrapper_enter", "plugin", 1.0, None, None, None, None),
        ("wrapper_enter", "plugin", 2.0, None, None, None, None),
        ("implementation_start", "plugin", 3.0, None, None, None, None),
        ("implementation_start", "plugin", 4.0, None, None, None, outer_call),
        ("implementation_end", "plugin", 5.0, 1.0, "inner result", None, outer_call),
        ("implementation_end", "plugin", 6.0, 3.0, "inner result", None, None),
        (
            "wrapper_exit",
            "plugin",
            7.0,
            5.0,
            "inner wrapper inner result",
            None,
            None,
        ),
        (
            "wrapper_exit",
            "plugin",
            8.0,
            7.0,
            "outer wrapper inner wrapper inner result",
            None,
            None,
        ),
    ]
    assert all(event.call_id in {outer_call, inner_call} for event in events)
    assert events[3].call_id == inner_call
    assert events[-1].call_id == outer_call

    undo()
    events.clear()
    assert pm.hook.outer() == "outer wrapper inner wrapper inner result"
    assert events == []

def test_firstresult_unexecuted_impl_has_no_events() -> None:
    pm = PluginManager("example")

    class Hooks:
        @hookspec(firstresult=True)
        def hook(self):
            pass

    pm.add_hookspecs(Hooks)
    events: list[HookImplEvent] = []
    pm.add_hookimpl_observer(events.append, time_source=FixedClock())

    class Plugin:
        @hookimpl(specname="hook", tryfirst=True)
        def first(self):
            return "first"

        @hookimpl(specname="hook")
        def second(self):
            return "second"

    pm.register(Plugin(), name="plugin")

    assert pm.hook.hook() == "first"
    assert [event.plugin_name for event in events] == ["plugin", "plugin"]


def test_observer_reports_old_style_hookwrapper() -> None:
    pm = PluginManager("example")

    class Hooks:
        @hookspec
        def hook(self):
            pass

    pm.add_hookspecs(Hooks)
    events: list[HookImplEvent] = []
    pm.add_hookimpl_observer(events.append, time_source=FixedClock())

    class Wrapper:
        @hookimpl(hookwrapper=True, tryfirst=True)
        def hook(self):
            yield

    class Plugin:
        @hookimpl
        def hook(self):
            return 1

    pm.register(Wrapper())
    pm.register(Plugin())

    assert pm.hook.hook() == [1]

    wrapper_name = pm.hook.hook.get_hookimpls()[1].plugin_name
    implementation_name = pm.hook.hook.get_hookimpls()[0].plugin_name
    assert [
        (event.event, event.plugin_name, event.elapsed, event.result)
        for event in events
    ] == [
        ("wrapper_enter", wrapper_name, None, None),
        ("implementation_start", implementation_name, None, None),
        ("implementation_end", implementation_name, 1.0, 1),
        ("wrapper_exit", wrapper_name, 3.0, [1]),
    ]


def test_observer_error_does_not_change_result() -> None:
    pm = PluginManager("example")

    class Hooks:
        @hookspec
        def hook(self):
            pass

    pm.add_hookspecs(Hooks)
    diagnostic: list[BaseException] = []

    def observer(event: HookImplEvent) -> None:
        raise RuntimeError("observer failed")

    def on_error(exc: BaseException, event: HookImplEvent) -> None:
        diagnostic.append(exc)

    pm.add_hookimpl_observer(observer, on_observer_error=on_error)

    class Plugin:
        @hookimpl
        def hook(self):
            return "plugin result"

    pm.register(Plugin())

    assert pm.hook.hook() == ["plugin result"]
    assert len(diagnostic) == 2
    assert all(str(exc) == "observer failed" for exc in diagnostic)


def test_observer_error_does_not_replace_plugin_exception() -> None:
    pm = PluginManager("example")

    class Hooks:
        @hookspec
        def hook(self):
            pass

    pm.add_hookspecs(Hooks)
    events: list[HookImplEvent] = []

    def on_error(exc: BaseException, event: HookImplEvent) -> None:
        pass

    def collecting_observer(event: HookImplEvent) -> None:
        events.append(event)
        raise RuntimeError("observer failed")

    pm.add_hookimpl_observer(collecting_observer, on_observer_error=on_error)

    class Plugin:
        @hookimpl
        def hook(self):
            raise ValueError("plugin failed")

    pm.register(Plugin())

    with pytest.raises(ValueError, match="plugin failed"):
        pm.hook.hook()

    assert len(events) == 2
    assert events[0].event == "implementation_start"
    assert events[0].exception is None
    assert events[1].event == "implementation_end"
    assert isinstance(events[1].exception, ValueError)
    assert events[1].exception.args == ("plugin failed",)


def test_observer_error_default_diagnostic(caplog: pytest.LogCaptureFixture) -> None:
    pm = PluginManager("example")

    class Hooks:
        @hookspec
        def hook(self):
            pass

    pm.add_hookspecs(Hooks)

    def observer(event: HookImplEvent) -> None:
        raise RuntimeError("observer failed")

    pm.add_hookimpl_observer(observer)

    class Plugin:
        @hookimpl
        def hook(self):
            return None

    pm.register(Plugin())

    with caplog.at_level(logging.WARNING, logger="pluggy._observation"):
        assert pm.hook.hook() == []

    assert "Hook implementation observer failed" in caplog.text


def test_disabled_observer_does_not_consume_time_source() -> None:
    pm = PluginManager("example")

    class Hooks:
        @hookspec
        def hook(self):
            pass

    pm.add_hookspecs(Hooks)
    clock_calls = 0

    def time_source() -> float:
        nonlocal clock_calls
        clock_calls += 1
        return float(clock_calls)

    undo = pm.add_hookimpl_observer(lambda event: None, time_source=time_source)
    undo()

    class Plugin:
        @hookimpl
        def hook(self):
            return 1

    pm.register(Plugin())

    assert pm.hook.hook() == [1]
    assert clock_calls == 0


def test_broken_time_source_uses_diagnostic_channel() -> None:
    pm = PluginManager("example")

    class Hooks:
        @hookspec
        def hook(self):
            pass

    pm.add_hookspecs(Hooks)
    diagnostic: list[BaseException] = []

    def time_source() -> float:
        raise RuntimeError("clock failed")

    pm.add_hookimpl_observer(
        lambda event: None,
        time_source=time_source,
        on_observer_error=lambda exc, event: diagnostic.append(exc),
    )

    class Plugin:
        @hookimpl
        def hook(self):
            return 1

    pm.register(Plugin())

    assert pm.hook.hook() == [1]
    assert len(diagnostic) == 1
    assert str(diagnostic[0]) == "clock failed"


def test_parent_call_does_not_skip_unobserved_manager() -> None:
    observed_manager = PluginManager("example")
    middle_manager = PluginManager("example")
    inner_manager = PluginManager("example")

    class ObservedHooks:
        @hookspec
        def outer(self):
            pass

    class MiddleHooks:
        @hookspec
        def middle(self):
            pass

    class InnerHooks:
        @hookspec
        def inner(self):
            pass

    observed_manager.add_hookspecs(ObservedHooks)
    middle_manager.add_hookspecs(MiddleHooks)
    inner_manager.add_hookspecs(InnerHooks)

    events: list[HookImplEvent] = []
    observed_manager.add_hookimpl_observer(events.append)

    class InnerPlugin:
        @hookimpl
        def inner(self):
            return "inner"

    class MiddlePlugin:
        @hookimpl
        def middle(self):
            return inner_manager.hook.inner()

    class OuterPlugin:
        @hookimpl
        def outer(self):
            return middle_manager.hook.middle()

    inner_manager.register(InnerPlugin())
    middle_manager.register(MiddlePlugin())
    observed_manager.register(OuterPlugin())

    assert observed_manager.hook.outer() == [[["inner"]]]

    outer_call = events[0].call_id
    assert len(events) == 2
    assert events[0].event == "implementation_start"
    assert events[0].call_id == outer_call
    assert events[0].parent_call_id is None
    assert events[1].event == "implementation_end"
    assert events[1].call_id == outer_call
    assert events[1].parent_call_id is None
