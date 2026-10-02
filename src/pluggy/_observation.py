"""Implementation-level hook observation."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from contextvars import Token
from dataclasses import dataclass
from itertools import count
import logging
from typing import TypeAlias

from ._impl import HookImpl


TimeSource: TypeAlias = Callable[[], float]


@dataclass(frozen=True, slots=True)
class HookImplEvent:
    """An event from executing one hook implementation.

    ``event`` is one of ``"implementation_start"``, ``"implementation_end"``,
    ``"wrapper_enter"`` and ``"wrapper_exit"``.

    ``call_id`` identifies the hook multicall executing the implementation.
    ``parent_call_id`` identifies a multicall invoked from inside the current
    multicall.
    """

    event: str
    hook_name: str
    hook_impl: HookImpl
    plugin_name: str
    call_id: int
    parent_call_id: int | None
    timestamp: float
    elapsed: float | None = None
    result: object | None = None
    exception: BaseException | None = None

    @property
    def wrapper(self) -> bool:
        return self.event.startswith("wrapper_")

    @property
    def phase(self) -> str:
        return self.event.split("_", 1)[1]

    @property
    def duration(self) -> float | None:
        return self.elapsed


HookImplObserver: TypeAlias = Callable[[HookImplEvent], None]
ObserverErrorHandler: TypeAlias = Callable[[BaseException, HookImplEvent], None]


@dataclass(frozen=True, slots=True)
class ObserverEntry:
    observer: HookImplObserver
    time_source: TimeSource
    on_error: ObserverErrorHandler


class HookCallObservation:
    """Observer state for one multicall."""

    __slots__ = ("call_id", "entries", "parent_call_id")

    def __init__(
        self,
        entries: tuple[ObserverEntry, ...],
        call_id: int,
        parent_call_id: int | None,
    ) -> None:
        self.entries = entries
        self.call_id = call_id
        self.parent_call_id = parent_call_id

    def report_observer_error(
        self,
        entry: ObserverEntry,
        exc: BaseException,
        event: HookImplEvent,
    ) -> None:
        try:
            entry.on_error(exc, event)
        except Exception:
            logging.getLogger(__name__).exception(
                "Hook implementation observer error handler failed"
            )

    def emit(
        self,
        event_name: str,
        hook_name: str,
        hook_impl: HookImpl,
        timestamp: float,
        *,
        elapsed: float | None = None,
        result: object | None = None,
        exception: BaseException | None = None,
    ) -> None:
        event = HookImplEvent(
            event=event_name,
            hook_name=hook_name,
            hook_impl=hook_impl,
            plugin_name=hook_impl.plugin_name,
            call_id=self.call_id,
            parent_call_id=self.parent_call_id,
            timestamp=timestamp,
            elapsed=elapsed,
            result=result,
            exception=exception,
        )
        for observer_entry in self.entries:
            try:
                observer_entry.observer(event)
            except Exception as observer_exc:
                self.report_observer_error(observer_entry, observer_exc, event)

    def span(self, hook_name: str, hook_impl: HookImpl) -> ObservedSpan:
        return ObservedSpan(self, hook_name, hook_impl)


class ObservedSpan:
    __slots__ = ("hook_impl", "hook_name", "observation", "start_times")

    def __init__(
        self,
        observation: HookCallObservation,
        hook_name: str,
        hook_impl: HookImpl,
    ) -> None:
        self.observation = observation
        self.hook_name = hook_name
        self.hook_impl = hook_impl
        self.start_times: list[float | None] = []
        for entry in observation.entries:
            self.start_times.append(self._read_time(entry))

    def _failure_event(
        self,
        event_name: str,
        timestamp: float,
        exc: BaseException,
    ) -> HookImplEvent:
        return HookImplEvent(
            event=event_name,
            hook_name=self.hook_name,
            hook_impl=self.hook_impl,
            plugin_name=self.hook_impl.plugin_name,
            call_id=self.observation.call_id,
            parent_call_id=self.observation.parent_call_id,
            timestamp=timestamp,
            exception=exc,
        )

    def _read_time(self, entry: ObserverEntry) -> float | None:
        try:
            return entry.time_source()
        except Exception as exc:
            self.observation.report_observer_error(
                entry,
                exc,
                self._failure_event("<observer-failure>", 0.0, exc),
            )
            return None

    def start(self, event_name: str) -> None:
        for entry, start_time in zip(self.observation.entries, self.start_times):
            if start_time is not None:
                self.observation.emit(
                    event_name,
                    self.hook_name,
                    self.hook_impl,
                    start_time,
                )

    def finish(
        self,
        event_name: str,
        *,
        result: object | None = None,
        exception: BaseException | None = None,
    ) -> None:
        for entry, start_time in zip(self.observation.entries, self.start_times):
            if start_time is None:
                continue
            try:
                end_time = entry.time_source()
            except Exception as exc:
                self.observation.report_observer_error(
                    entry,
                    exc,
                    self._failure_event("<observer-failure>", start_time, exc),
                )
                continue
            self.observation.emit(
                event_name,
                self.hook_name,
                self.hook_impl,
                end_time,
                elapsed=end_time - start_time,
                result=result,
                exception=exception,
            )


_current_observation: ContextVar[HookCallObservation | None] = ContextVar(
    "pluggy_hook_call_observation", default=None
)
_call_ids = count(1)


def next_call_id() -> int:
    return next(_call_ids)


def current_hook_call_observation() -> HookCallObservation | None:
    return _current_observation.get()


def current_hook_call_id() -> int | None:
    observation = _current_observation.get()
    return None if observation is None else observation.call_id


def set_current_hook_call_observation(
    observation: HookCallObservation,
) -> Token[HookCallObservation | None]:
    return _current_observation.set(observation)


def reset_current_hook_call_observation(
    token: Token[HookCallObservation | None],
) -> None:
    _current_observation.reset(token)


def log_observer_error(exc: BaseException, event: HookImplEvent) -> None:
    """Send hook implementation observer errors to the logging diagnostic channel."""
    logging.getLogger(__name__).warning(
        "Hook implementation observer failed for hook %r and plugin %r",
        event.hook_name,
        event.plugin_name,
        exc_info=exc,
    )
