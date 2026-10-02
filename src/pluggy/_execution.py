"""
Hook call execution (multicall) machinery.
"""

from __future__ import annotations

from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from typing import cast
from typing import NoReturn
from typing import TYPE_CHECKING
from typing import TypeAlias
import warnings

from . import _observability
from ._impl import HookImpl
from ._result import HookCallError
from ._result import Result
from ._warnings import PluggyTeardownRaisedWarning


# Need to distinguish between old- and new-style hook wrappers.
# Wrapping with a tuple is the fastest type-safe way I found to do it.
Teardown: TypeAlias = Generator[None, object, object]


def run_old_style_hookwrapper(
    hook_impl: HookImpl,
    hook_name: str,
    args: Sequence[object],
    kwargs: Mapping[str, object] | None,
) -> Teardown:
    """
    backward compatibility wrapper to run a old style hookwrapper as a wrapper
    """
    if kwargs is None:
        res = hook_impl.function(*args)
    else:
        res = hook_impl.function(*args, **kwargs)
    if TYPE_CHECKING:
        teardown = cast(Teardown, res)
    else:
        teardown = res
    try:
        next(teardown)
    except StopIteration:
        _raise_wrapfail(teardown, "did not yield")
    try:
        res = yield
        result = Result(res, None)
    except BaseException as exc:
        result = Result(None, exc)
    try:
        teardown.send(result)
    except StopIteration:
        pass
    except BaseException as e:
        _warn_teardown_exception(hook_name, hook_impl, e)
        raise
    else:
        _raise_wrapfail(teardown, "has second yield")
    finally:
        teardown.close()
    return result.get_result()


def _raise_wrapfail(
    wrap_controller: Generator[None, object, object],
    msg: str,
) -> NoReturn:
    co = wrap_controller.gi_code  # type: ignore[attr-defined]
    raise RuntimeError(
        f"wrap_controller at {co.co_name!r} {co.co_filename}:{co.co_firstlineno} {msg}"
    )


def _warn_teardown_exception(
    hook_name: str, hook_impl: HookImpl, e: BaseException
) -> None:
    msg = (
        f"A plugin raised an exception during an old-style hookwrapper teardown.\n"
        f"Plugin: {hook_impl.plugin_name}, Hook: {hook_name}\n"
        f"{type(e).__name__}: {e}\n"
        f"For more information see https://pluggy.readthedocs.io/en/stable/api_reference.html#pluggy.PluggyTeardownRaisedWarning"
    )
    warnings.warn(PluggyTeardownRaisedWarning(msg), stacklevel=6)


def _multicall(
    hook_name: str,
    hook_impls: Sequence[HookImpl],
    caller_kwargs: Mapping[str, object],
    firstresult: bool,
    observers: _observability.Sequence[_observability.ObserverEntry] = (),
) -> object | list[object]:
    """Execute a call into multiple python functions/methods and return the
    result(s).

    ``caller_kwargs`` comes from HookCaller.__call__().

    ``observers`` is an optional sequence of ``(observer, clock)`` pairs
    installed via :meth:`pluggy.PluginManager.add_hookimpl_observer`.
    """
    __tracebackhide__ = True

    # Observation is opt-in: with no observers, ``sessions`` stays empty and
    # every observability helper becomes a no-op, so no event objects or even
    # timestamp tuples are constructed.
    if observers:
        sessions, call_id, start_times = _observability.begin_call(
            observers, hook_name
        )
    else:
        sessions = ()
        call_id = -1
        start_times = ()
    observing = bool(sessions)

    results: list[object] = []
    exception = None
    teardowns: list[tuple[HookImpl, Teardown]] = []

    token = _observability.set_parent_call_id(call_id) if observing else None
    try:
        _observability.emit_hook_call(sessions, start_times)
        # run impl and wrapper setup functions in a loop
        for hook_impl in reversed(hook_impls):
            try:
                args = [caller_kwargs[argname] for argname in hook_impl.argnames]
                if hook_impl.kwargnames:
                    kwargs = {
                        argname: caller_kwargs[argname]
                        for argname in hook_impl.kwargnames
                        if argname in caller_kwargs
                    } or None
                else:
                    kwargs = None
            except KeyError as e:
                raise HookCallError(
                    f"hook call must provide argument {e.args[0]!r}"
                ) from e

            if hook_impl.hookwrapper:
                phase = _observability.PHASE_WRAPPER_ENTER
                phase_starts = _observability.start_phase(
                    sessions, hook_name, hook_impl, phase
                )
                try:
                    function_gen = run_old_style_hookwrapper(
                        hook_impl, hook_name, args, kwargs
                    )
                    next(function_gen)  # first yield
                except BaseException as exc:
                    _observability.exception_phase(
                        sessions,
                        hook_name,
                        hook_impl,
                        phase,
                        phase_starts,
                        exc,
                    )
                    raise
                _observability.end_phase(
                    sessions,
                    hook_name,
                    hook_impl,
                    phase,
                    phase_starts,
                    None,
                )
                teardowns.append((hook_impl, function_gen))

            elif hook_impl.wrapper:
                phase = _observability.PHASE_WRAPPER_ENTER
                phase_starts = _observability.start_phase(
                    sessions, hook_name, hook_impl, phase
                )
                try:
                    if kwargs is None:
                        res = hook_impl.function(*args)
                    else:
                        res = hook_impl.function(*args, **kwargs)
                    # If this cast is not valid, a type error is raised below,
                    # which is the desired response.
                    if TYPE_CHECKING:
                        function_gen = cast(Generator[None, object, object], res)
                    else:
                        function_gen = res
                    next(function_gen)  # first yield
                except StopIteration as exc:
                    _observability.exception_phase(
                        sessions,
                        hook_name,
                        hook_impl,
                        phase,
                        phase_starts,
                        exc,
                    )
                    _raise_wrapfail(function_gen, "did not yield")
                except BaseException as exc:
                    _observability.exception_phase(
                        sessions,
                        hook_name,
                        hook_impl,
                        phase,
                        phase_starts,
                        exc,
                    )
                    raise
                _observability.end_phase(
                    sessions,
                    hook_name,
                    hook_impl,
                    phase,
                    phase_starts,
                    None,
                )
                teardowns.append((hook_impl, function_gen))
            else:
                phase = _observability.PHASE_IMPL
                phase_starts = _observability.start_phase(
                    sessions, hook_name, hook_impl, phase
                )
                try:
                    if kwargs is None:
                        res = hook_impl.function(*args)
                    else:
                        res = hook_impl.function(*args, **kwargs)
                except BaseException as exc:
                    _observability.exception_phase(
                        sessions,
                        hook_name,
                        hook_impl,
                        phase,
                        phase_starts,
                        exc,
                    )
                    raise
                _observability.end_phase(
                    sessions,
                    hook_name,
                    hook_impl,
                    phase,
                    phase_starts,
                    res,
                )
                if res is not None:
                    results.append(res)
                    if firstresult:  # halt further impl calls
                        break
    except BaseException as exc:
        exception = exc
    finally:
        if firstresult:  # first result hooks return a single value
            result = results[0] if results else None
        else:
            result = results

        # run all wrapper post-yield blocks
        for hook_impl, teardown in reversed(teardowns):
            phase = _observability.PHASE_WRAPPER_EXIT
            phase_starts = _observability.start_phase(
                sessions, hook_name, hook_impl, phase
            )
            try:
                if exception is not None:
                    try:
                        teardown.throw(exception)
                    except RuntimeError as re:
                        # StopIteration from generator causes RuntimeError
                        # even for coroutine usage - see #544
                        if (
                            isinstance(exception, StopIteration)
                            and re.__cause__ is exception
                        ):
                            _observability.end_phase(
                                sessions,
                                hook_name,
                                hook_impl,
                                phase,
                                phase_starts,
                                None,
                            )
                            teardown.close()
                            continue
                        else:
                            raise
                else:
                    teardown.send(result)
                # Following is unreachable for a well behaved hook wrapper.
                # Try to force finalizers otherwise postponed till GC action.
                # Note: close() may raise if generator handles GeneratorExit.
                teardown.close()
            except StopIteration as si:
                _observability.end_phase(
                    sessions,
                    hook_name,
                    hook_impl,
                    phase,
                    phase_starts,
                    si.value,
                )
                result = si.value
                exception = None
                continue
            except BaseException as e:
                _observability.exception_phase(
                    sessions,
                    hook_name,
                    hook_impl,
                    phase,
                    phase_starts,
                    e,
                )
                exception = e
                continue
            _raise_wrapfail(teardown, "has second yield")

        _observability.emit_hook_call_result(
            sessions, start_times, exception
        )
        if token is not None:
            _observability.reset_parent_call_id(token)

    if exception is not None:
        raise exception
    else:
        return result

