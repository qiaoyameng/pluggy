"""
Hook call execution (multicall) machinery.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from collections.abc import Coroutine
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from types import AsyncGeneratorType
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

# Async generator wrappers, the asynchronous counterpart of `Teardown`.
AsyncTeardown: TypeAlias = AsyncGenerator[None, object]


def _hookimpl_args(
    hook_impl: HookImpl,
    caller_kwargs: Mapping[str, object],
) -> tuple[list[object], dict[str, object] | None]:
    """Bind the positional/keyword arguments for a hook implementation."""
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
    return args, kwargs


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
    wrap_controller: Generator[None, object, object] | AsyncGenerator[None, object],
    msg: str,
) -> NoReturn:
    co = getattr(wrap_controller, "gi_code", None) or wrap_controller.ag_code  # type: ignore[union-attr]
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
    results: list[object] = []
    exception = None
    teardowns: list[Teardown] = []
    try:  # run impl and wrapper setup functions in a loop
        for hook_impl in reversed(hook_impls):
            if hook_impl.iscoroutinefunction or hook_impl.isasyncgenfunction:
                raise HookCallError(
                    f"hook implementation {hook_impl.function.__name__!r} "
                    f"of plugin {hook_impl.plugin_name!r} for hook "
                    f"{hook_name!r} is asynchronous and cannot be called "
                    "synchronously; use `await hook.acall(...)` instead"
                )
            args, kwargs = _hookimpl_args(hook_impl, caller_kwargs)

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


async def _amulticall(
    hook_name: str,
    hook_impls: Sequence[HookImpl],
    caller_kwargs: Mapping[str, object],
    firstresult: bool,
) -> object | list[object]:
    """Execute a call into multiple python functions/methods and return the
    result(s), awaiting asynchronous implementations.

    This is the asynchronous counterpart of :func:`_multicall`. Coroutine
    implementations are awaited and async generator wrappers are driven
    with ``asend``/``athrow``, while plain synchronous implementations and
    generator wrappers participate unchanged. Implementations run serially
    in the established call order and ``firstresult`` keeps its meaning.

    Async generator wrappers follow a protocol mirroring the synchronous
    new-style wrappers: the wrapper runs until its first ``yield`` during
    setup; the inner result is then delivered through that ``yield`` (or an
    exception, including :exc:`asyncio.CancelledError`, is thrown in). Since
    async generators cannot ``return`` a value, a wrapper replaces the
    result by yielding it a second (and final) time instead of returning
    it; finishing without a second ``yield`` propagates the current result
    or exception unchanged.

    ``caller_kwargs`` comes from HookCaller.acall().
    """
    __tracebackhide__ = True
    results: list[object] = []
    exception = None
    teardowns: list[Teardown | AsyncTeardown] = []
    try:  # run impl and wrapper setup functions in a loop
        for hook_impl in reversed(hook_impls):
            args, kwargs = _hookimpl_args(hook_impl, caller_kwargs)

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
                if hook_impl.isasyncgenfunction:
                    if TYPE_CHECKING:
                        async_teardown = cast(AsyncTeardown, res)
                    else:
                        async_teardown = res
                    try:
                        await async_teardown.__anext__()  # first yield
                    except StopAsyncIteration:
                        _raise_wrapfail(async_teardown, "did not yield")
                    teardowns.append(async_teardown)
                else:
                    # If this cast is not valid, a type error is raised below,
                    # which is the desired response.
                    if TYPE_CHECKING:
                        function_gen = cast(Teardown, res)
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
                if hook_impl.iscoroutinefunction:
                    res = await cast(Coroutine[object, object, object], res)
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

        # run all wrapper post-yield blocks, innermost first
        for teardown in reversed(teardowns):
            if isinstance(teardown, AsyncGeneratorType):
                try:
                    if exception is not None:
                        replacement = await teardown.athrow(exception)
                    else:
                        replacement = await teardown.asend(result)
                except StopAsyncIteration:
                    # The wrapper finished without yielding a replacement;
                    # the current result/exception propagates unchanged.
                    continue
                except BaseException as e:
                    exception = e
                    continue
                # The wrapper yielded a replacement result.
                result = replacement
                exception = None
                try:
                    await teardown.__anext__()
                except StopAsyncIteration:
                    continue
                except BaseException as e:
                    exception = e
                    continue
                # Following is unreachable for a well behaved async wrapper.
                # Try to force finalizers otherwise postponed till GC action.
                try:
                    await teardown.aclose()
                except BaseException as e:
                    exception = e
                    continue
                _raise_wrapfail(teardown, "has third yield")
            else:
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
