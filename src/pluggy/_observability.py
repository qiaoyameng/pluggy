"""
Implementation-level observation.

Unlike :meth:`pluggy.PluginManager.add_hookcall_monitoring`, which only
reports around an entire hook call, an observer reports the lifetime of every
individual hook implementation that actually runs: plain implementations, as
well as the separate entry (pre-yield) and exit (post-yield) phases of
wrappers.

Observation is opt-in and streaming: when no observer is installed the
execution engine performs no allocations for events, and event objects are
never retained in a list by pluggy itself.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from itertools import count
from typing import Any
from typing import final
from typing import Protocol
import warnings

from ._impl import HookImpl
from ._warnings import PluggyWarning


#: Phase of a plain (non-wrapper) hook implementation.
PHASE_IMPL = "impl"
#: Entry phase of a wrapper: code before the ``yield``.
PHASE_WRAPPER_ENTER = "wrapper_enter"
#: Exit phase of a wrapper: code after the ``yield``.
PHASE_WRAPPER_EXIT = "wrapper_exit"

Phase = str

#: Returns timestamps; inject any monotonic source (used by tests).
Clock = Callable[[], float]

#: Identity of a single hook call. Nested hook calls carry a distinct
#: call id while recording the id of the call that triggered them.
CallId = int


@final
class PluggyObserverWarning(PluggyWarning):
    """An observer raised an exception while reporting an event.

    Observer failures are routed here instead of being propagated, so they
    never mask a plugin's own exception or alter a hook call's result.
    """

    __module__ = "pluggy"


@final
@dataclass(frozen=True, slots=True)
class HookCallEvent:
    """A hook call started."""

    call_id: CallId
    hook_name: str
    #: Id of the hook call during which this call happened, or ``None``.
    parent_call_id: CallId | None


@final
@dataclass(frozen=True, slots=True)
class HookCallResultEvent:
    """A hook call finished."""

    call_id: CallId
    hook_name: str
    #: Timestamps produced by the observer's injected clock.
    start_time: float
    end_time: float
    #: The exception that escaped the hook call, or ``None`` on success.
    exception: BaseException | None


@final
@dataclass(frozen=True, slots=True)
class ImplStartEvent:
    """A hook implementation phase started."""

    call_id: CallId
    hook_name: str
    impl: HookImpl
    #: One of :data:`PHASE_IMPL`, :data:`PHASE_WRAPPER_ENTER`,
    #: :data:`PHASE_WRAPPER_EXIT`.
    phase: Phase
    start_time: float


@final
@dataclass(frozen=True, slots=True)
class ImplEndEvent:
    """A hook implementation phase finished without raising."""

    call_id: CallId
    hook_name: str
    impl: HookImpl
    phase: Phase
    start_time: float
    end_time: float
    #: Value returned by the phase:
    #:
    #: * ``None`` for :data:`PHASE_WRAPPER_ENTER` (the generated wrapper
    #:   itself is not reported),
    #: * the implementation's return value for :data:`PHASE_IMPL`,
    #: * the wrapper's return value for :data:`PHASE_WRAPPER_EXIT`.
    result: Any


@final
@dataclass(frozen=True, slots=True)
class ImplExceptionEvent:
    """A hook implementation phase raised an exception."""

    call_id: CallId
    hook_name: str
    impl: HookImpl
    phase: Phase
    start_time: float
    end_time: float
    exception: BaseException


class HookImplObserver(Protocol):
    """Streaming observer for hook implementation execution.

    All methods default to no-ops, so observers can inherit from the protocol
    and override only the callbacks they care about::

        class Recorder(pluggy.HookImplObserver):
            def on_impl_start(self, event):
                ...

    Exceptions raised by observer methods are not propagated: they are routed
    through :meth:`on_observer_error` (which, by default, emits a
    :class:`PluggyObserverWarning`) and swallowed, so they can never override a
    plugin's own exception or a hook call's result.
    """

    def on_hook_call(self, event: HookCallEvent) -> None:
        """Called once when a hook call begins."""

    def on_hook_call_result(self, event: HookCallResultEvent) -> None:
        """Called once when a hook call ends, even when it raised."""

    def on_impl_start(self, event: ImplStartEvent) -> None:
        """Called when an implementation phase begins."""

    def on_impl_end(self, event: ImplEndEvent) -> None:
        """Called when an implementation phase returns normally."""

    def on_impl_exception(self, event: ImplExceptionEvent) -> None:
        """Called when an implementation phase raises."""

    def on_observer_error(self, callback_name: str, exc: BaseException) -> None:
        """Called when another observer method raised.

        Override to redirect observer diagnostics, for example to a logger.
        Exceptions raised here are swallowed as well. The default
        implementation emits a :class:`PluggyObserverWarning`.
        """
        warnings.warn(
            f"{callback_name} raised {type(exc).__name__}: {exc}",
            PluggyObserverWarning,
            stacklevel=2,
        )


# An observer paired with the clock producing its timestamps.
ObserverEntry = tuple[HookImplObserver, Clock]
# Sessions of one hook call, one per observing installation.
Sessions = tuple["_ObservationSession", ...]

_call_ids = count(1)
#: Id of the hook call during which code is currently executing, if any.
#: Only observed calls set it; unobserved calls leave the outer value in
#: place so a deeply nested observed call still links to its observed parent.
_parent_call_id: ContextVar[CallId | None] = ContextVar(
    "pluggy_parent_call_id", default=None
)


@final
class _ObservationSession:
    """One (observer, clock) pair observing a single hook call."""

    __slots__ = ("call_id", "clock", "hook_name", "observer", "parent_call_id")

    def __init__(
        self,
        observer: HookImplObserver,
        clock: Clock,
        call_id: CallId,
        hook_name: str,
        parent_call_id: CallId | None,
    ) -> None:
        self.observer = observer
        self.clock = clock
        self.call_id = call_id
        self.hook_name = hook_name
        self.parent_call_id = parent_call_id

    def now(self) -> float:
        try:
            return self.clock()
        except BaseException as exc:
            self.report("clock", exc)
            return float("nan")

    def report(self, callback_name: str, exc: BaseException) -> None:
        try:
            self.observer.on_observer_error(callback_name, exc)
        except BaseException:
            pass

    def emit(self, callback_name: str, event: Any) -> None:
        try:
            getattr(self.observer, callback_name)(event)
        except BaseException as exc:
            self.report(callback_name, exc)


def begin_call(
    observers: Sequence[ObserverEntry],
    hook_name: str,
) -> tuple[Sessions, CallId, tuple[float, ...]]:
    """Create sessions for an observed hook call.

    Returns ``(sessions, call_id, start_times)``. All sessions share one
    ``call_id``; each carries its own clock's start timestamp.
    """
    parent_call_id = _parent_call_id.get()
    call_id = next(_call_ids)
    sessions = tuple(
        _ObservationSession(observer, clock, call_id, hook_name, parent_call_id)
        for observer, clock in observers
    )
    start_times = tuple(session.now() for session in sessions)
    return sessions, call_id, start_times


def set_parent_call_id(call_id: CallId) -> Any:
    return _parent_call_id.set(call_id)


def reset_parent_call_id(token: Any) -> None:
    _parent_call_id.reset(token)


def emit_hook_call(
    sessions: Sessions, start_times: tuple[float, ...]
) -> None:
    for session, start_time in zip(sessions, start_times):
        session.emit(
            "on_hook_call",
            HookCallEvent(session.call_id, session.hook_name, session.parent_call_id),
        )


def emit_hook_call_result(
    sessions: Sessions,
    start_times: tuple[float, ...],
    exception: BaseException | None,
) -> None:
    for session, start_time in zip(sessions, start_times):
        session.emit(
            "on_hook_call_result",
            HookCallResultEvent(
                session.call_id,
                session.hook_name,
                start_time,
                session.now(),
                exception,
            ),
        )


def start_phase(
    sessions: Sessions,
    hook_name: str,
    hook_impl: HookImpl,
    phase: Phase,
) -> tuple[float, ...] | None:
    """Emit ``on_impl_start`` and return per-session start timestamps."""
    if not sessions:
        return None
    phase_starts = tuple(session.now() for session in sessions)
    for session, phase_start in zip(sessions, phase_starts):
        session.emit(
            "on_impl_start",
            ImplStartEvent(
                session.call_id,
                hook_name,
                hook_impl,
                phase,
                phase_start,
            ),
        )
    return phase_starts


def end_phase(
    sessions: Sessions,
    hook_name: str,
    hook_impl: HookImpl,
    phase: Phase,
    phase_starts: tuple[float, ...] | None,
    result: object,
) -> None:
    if phase_starts is None:
        return
    for session, phase_start in zip(sessions, phase_starts):
        session.emit(
            "on_impl_end",
            ImplEndEvent(
                session.call_id,
                hook_name,
                hook_impl,
                phase,
                phase_start,
                session.now(),
                result,
            ),
        )


def exception_phase(
    sessions: Sessions,
    hook_name: str,
    hook_impl: HookImpl,
    phase: Phase,
    phase_starts: tuple[float, ...] | None,
    exc: BaseException,
) -> None:
    if phase_starts is None:
        return
    for session, phase_start in zip(sessions, phase_starts):
        session.emit(
            "on_impl_exception",
            ImplExceptionEvent(
                session.call_id,
                hook_name,
                hook_impl,
                phase,
                phase_start,
                session.now(),
                exc,
            ),
        )
