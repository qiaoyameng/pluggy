"""
Hook call execution (multicall) machinery.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from typing import cast
from typing import NoReturn
from typing import TYPE_CHECKING
from typing import TypeAlias
import warnings

from ._impl import HookImpl
from ._result import HookCallError
from ._result import Result
from ._warnings import PluggyTeardownRaisedWarning


# Need to distinguish between old- and new-style hook wrappers.
# Wrapping with a tuple is the fastest type-safe way I found to do it.
Teardown: TypeAlias = Generator[None, object, object]
AsyncTeardown: TypeAlias = AsyncGenerator[None, object]

#: ``(kind, teardown)`` pairs collected while running an asynchronous
#: multicall. ``kind`` is 0 for a synchronous generator (old- or new-style),
#: 1 for a new-style asynchronous generator, and 2 for the old-style
#: asynchronous wrapper adapter :class:`_AsyncOldStyleHookWrapper`.
_AsyncTeardownEntry: TypeAlias = tuple[
    int,
    "Teardown | AsyncTeardown | _AsyncOldStyleHookWrapper",
]


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
    wrap_controller: "Generator[None, object, object] | AsyncTeardown",
    msg: str,
) -> NoReturn:
    co = getattr(wrap_controller, "gi_code", None) or wrap_controller.ag_code
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
) -> object | list[object]:
    """Execute a call into multiple python functions/methods and return the
    result(s).

    ``caller_kwargs`` comes from HookCaller.__call__().
    """
    __tracebackhide__ = True
    async_impl = next(
        (
            impl
            for impl in hook_impls
            if impl.async_function or impl.async_generator_function
        ),
        None,
    )
    if async_impl is not None:
        raise HookCallError(
            f"hook {hook_name!r} has an asynchronous implementation "
            f"(plugin {async_impl.plugin_name!r}) and must be awaited with "
            f"'await <pm>.hook.{hook_name}.acall(...)' instead of being called "
            "synchronously"
        )
    results: list[object] = []
    exception = None
    teardowns: list[Teardown] = []
    try:  # run impl and wrapper setup functions in a loop
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
                function_gen = run_old_style_hookwrapper(
                    hook_impl, hook_name, args, kwargs
                )

                next(function_gen)  # first yield
                teardowns.append(function_gen)

            elif hook_impl.wrapper:
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
                try:
                    next(function_gen)  # first yield
                except StopIteration:
                    _raise_wrapfail(function_gen, "did not yield")
                teardowns.append(function_gen)
            else:
                if kwargs is None:
                    res = hook_impl.function(*args)
                else:
                    res = hook_impl.function(*args, **kwargs)
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
        for teardown in reversed(teardowns):
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
                result = si.value
                exception = None
                continue
            except BaseException as e:
                exception = e
                continue
            _raise_wrapfail(teardown, "has second yield")

    if exception is not None:
        raise exception
    else:
        return result


class _AsyncOldStyleHookWrapper:
    """Drive an old-style asynchronous hook wrapper.

    The plugin's async generator yields exactly once and receives a
    :class:`Result` (see :ref:`old_style_hookwrapper`). This adapter exposes
    the same "send/throw" protocol the multicall teardown loop uses for the
    new-style wrappers:

    * :meth:`asend` with the inner outcome;
    * :meth:`athrow` with an inner exception;
    * :meth:`aclose` at the end.

    Unlike new-style asynchronous wrappers (which cannot ``return`` a value,
    since ``return value`` is illegal in an asynchronous generator), an
    old-style wrapper replaces the result through
    :meth:`Result.force_result` / :meth:`Result.force_exception`. That final
    outcome is returned from :meth:`asend`/:meth:`athrow` (raising the forced
    exception if any), exactly like the ``return value`` of a synchronous
    generator surfaces as ``StopIteration.value``.
    """

    __slots__ = ("_result", "_teardown", "hook_impl", "hook_name")

    def __init__(
        self,
        hook_impl: HookImpl,
        hook_name: str,
        teardown: AsyncGenerator[None, Result[object]],
    ) -> None:
        self.hook_impl = hook_impl
        self.hook_name = hook_name
        self._teardown = teardown
        self._result: Result[object] | None = None

    async def start(self) -> None:
        """Run the wrapper up to its single (first) yield."""
        await anext(self._teardown)

    async def asend(self, value: object) -> object:
        self._result = Result(value, None)
        return await self._finish()

    async def athrow(self, exception: BaseException) -> object:
        self._result = Result(None, exception)
        return await self._finish()

    async def _finish(self) -> object:
        assert self._result is not None
        try:
            await self._teardown.asend(self._result)
        except StopAsyncIteration:
            # Well-behaved old-style wrapper: its single yield is done.
            # Surface whatever outcome the Result holds, including a value
            # or exception set via force_result()/force_exception().
            return self._result.get_result()
        except BaseException as e:
            _warn_teardown_exception(self.hook_name, self.hook_impl, e)
            raise
        else:
            _raise_wrapfail(self._teardown, "has second yield")

    async def aclose(self) -> None:
        await self._teardown.aclose()

    @property
    def ag_code(self) -> object:
        return self._teardown.ag_code


async def _async_multicall(
    hook_name: str,
    hook_impls: Sequence[HookImpl],
    caller_kwargs: Mapping[str, object],
    firstresult: bool,
) -> object | list[object]:
    """Asynchronous counterpart of :func:`_multicall`.

    Hook implementations are awaited one after another, in the same order as
    in a synchronous call. Both synchronous implementations (plain values)
    and coroutine implementations take part, as do synchronous and
    asynchronous wrappers.
    """
    __tracebackhide__ = True
    results: list[object] = []
    exception = None
    teardowns: list[_AsyncTeardownEntry] = []
    try:  # run impl and wrapper setup functions in a loop
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

            if hook_impl.hookwrapper and hook_impl.async_generator_function:
                if kwargs is None:
                    raw = hook_impl.function(*args)
                else:
                    raw = hook_impl.function(*args, **kwargs)
                function_gen = _AsyncOldStyleHookWrapper(
                    hook_impl,
                    hook_name,
                    cast(AsyncGenerator[None, Result[object]], raw),
                )
                await function_gen.start()
                teardowns.append((2, function_gen))

            elif hook_impl.wrapper and hook_impl.async_generator_function:
                if kwargs is None:
                    res = hook_impl.function(*args)
                else:
                    res = hook_impl.function(*args, **kwargs)
                # If this cast is not valid, a type error is raised below,
                # which is the desired response.
                function_gen = cast(AsyncTeardown, res)
                try:
                    await anext(function_gen)  # first yield
                except StopAsyncIteration:
                    _raise_wrapfail(function_gen, "did not yield")
                teardowns.append((1, function_gen))

            elif hook_impl.hookwrapper or hook_impl.wrapper:
                # Synchronous wrappers are driven step by step without
                # blocking the event loop.
                if hook_impl.hookwrapper:
                    function_gen_sync = run_old_style_hookwrapper(
                        hook_impl, hook_name, args, kwargs
                    )
                elif kwargs is None:
                    function_gen_sync = cast(Teardown, hook_impl.function(*args))
                else:
                    function_gen_sync = cast(
                        Teardown, hook_impl.function(*args, **kwargs)
                    )
                try:
                    next(function_gen_sync)  # first yield
                except StopIteration:
                    _raise_wrapfail(function_gen_sync, "did not yield")
                teardowns.append((0, function_gen_sync))

            else:
                if kwargs is None:
                    res = hook_impl.function(*args)
                else:
                    res = hook_impl.function(*args, **kwargs)
                if hook_impl.async_function:
                    if TYPE_CHECKING:
                        res = await cast(Awaitable[object], res)
                    else:
                        res = await res
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

        # run all wrapper post-yield blocks, outermost last
        for teardown_kind, teardown in reversed(teardowns):
            if teardown_kind == 0:
                # Synchronous generator (old- or new-style). A ``return value``
                # (StopIteration.value) replaces the inner result.
                sync_teardown = cast(Teardown, teardown)
                try:
                    if exception is not None:
                        try:
                            sync_teardown.throw(exception)
                        except RuntimeError as re:
                            # StopIteration from generator causes RuntimeError
                            # even for coroutine usage - see #544
                            if (
                                isinstance(exception, StopIteration)
                                and re.__cause__ is exception
                            ):
                                sync_teardown.close()
                                continue
                            else:
                                raise
                    else:
                        sync_teardown.send(result)
                    sync_teardown.close()
                except StopIteration as si:
                    result = si.value
                    exception = None
                    continue
                except BaseException as e:
                    exception = e
                    continue
                _raise_wrapfail(sync_teardown, "has second yield")

            elif teardown_kind == 1:
                # New-style asynchronous generator. ``return value`` is illegal
                # in an asynchronous generator, so falling through after the
                # single yield keeps the inner result (and an inner exception
                # keeps propagating). Only an explicit raise in the teardown
                # changes the outcome.
                native_teardown = cast(AsyncTeardown, teardown)
                finished = False
                try:
                    if exception is not None:
                        try:
                            await native_teardown.athrow(exception)
                        except RuntimeError as re:
                            # StopIteration/StopAsyncIteration from a generator
                            # causes RuntimeError even for coroutine usage -
                            # see #544
                            cause = re.__cause__
                            if (
                                isinstance(
                                    exception, (StopIteration, StopAsyncIteration)
                                )
                                and cause is exception
                            ):
                                await native_teardown.aclose()
                                continue
                            else:
                                raise
                        except StopAsyncIteration:
                            # Teardown finished: keep the inner outcome.
                            finished = True
                    else:
                        try:
                            await native_teardown.asend(result)
                        except StopAsyncIteration:
                            # Teardown finished: keep the inner result.
                            finished = True
                    if not finished:
                        # Following is unreachable for a well behaved hook
                        # wrapper. Force finalizers otherwise postponed till GC.
                        try:
                            await native_teardown.aclose()
                        except StopAsyncIteration:
                            pass
                    else:
                        continue
                except BaseException as e:
                    exception = e
                    continue
                _raise_wrapfail(native_teardown, "has second yield")

            else:
                # Old-style asynchronous hook wrapper, driven through the
                # adapter. Its (possibly forced) Result outcome replaces the
                # inner result, or raises.
                old_teardown = cast(_AsyncOldStyleHookWrapper, teardown)
                try:
                    if exception is not None:
                        result = await old_teardown.athrow(exception)
                    else:
                        result = await old_teardown.asend(result)
                    exception = None
                except BaseException as e:
                    exception = e
                    continue
                continue

    if exception is not None:
        raise exception
    else:
        return result
