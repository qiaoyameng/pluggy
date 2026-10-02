"""
Hook markers, specifications, and related helpers.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
import inspect
import sys
import types
from types import ModuleType
from typing import Final
from typing import final
from typing import overload
from typing import TypeAlias
from typing import TypeVar
import warnings

from ._config import HookimplOpts
from ._config import HookspecOpts


_F = TypeVar("_F", bound=Callable[..., object])

_Namespace: TypeAlias = ModuleType | type


@final
class HookParameters:
    """Analyzed parameters of a hook function or hook specification."""

    __slots__ = (
        "argdefaults",
        "explicit_argnames",
        "kwonly_argdefaults",
        "kwonly_argnames",
        "kwonly_kwargnames",
        "positional_only_argnames",
        "positional_kwargnames",
        "positional_argnames",
        "var_keyword",
    )

    def __init__(
        self,
        positional_argnames: tuple[str, ...],
        positional_kwargnames: tuple[str, ...],
        positional_only_argnames: tuple[str, ...],
        kwonly_argnames: tuple[str, ...],
        kwonly_kwargnames: tuple[str, ...],
        argdefaults: Mapping[str, object],
        kwonly_argdefaults: Mapping[str, object],
        var_keyword: bool,
    ) -> None:
        explicit_argnames = frozenset(
            positional_argnames
            + positional_kwargnames
            + kwonly_argnames
            + kwonly_kwargnames
        )
        self.positional_argnames: Final = positional_argnames
        self.positional_kwargnames: Final = positional_kwargnames
        self.positional_only_argnames: Final = positional_only_argnames
        self.kwonly_argnames: Final = kwonly_argnames
        self.kwonly_kwargnames: Final = kwonly_kwargnames
        self.argdefaults: Final[Mapping[str, object]] = dict(argdefaults)
        self.kwonly_argdefaults: Final[Mapping[str, object]] = dict(
            kwonly_argdefaults
        )
        self.var_keyword: Final = var_keyword
        self.explicit_argnames: Final[frozenset[str]] = explicit_argnames

    @property
    def argnames(self) -> tuple[str, ...]:
        return self.positional_argnames + self.kwonly_argnames

    @property
    def kwargnames(self) -> tuple[str, ...]:
        return self.positional_kwargnames + self.kwonly_kwargnames

    @property
    def all_argnames(self) -> tuple[str, ...]:
        return (
            self.positional_argnames
            + self.positional_kwargnames
            + self.kwonly_argnames
            + self.kwonly_kwargnames
        )


@final
class HookspecMarker:
    """Decorator for marking functions as hook specifications.

    Instantiate it with a project_name to get a decorator.
    Calling :meth:`PluginManager.add_hookspecs` later will discover all marked
    functions if the :class:`PluginManager` uses the same project name.
    """

    __slots__ = ("project_name",)

    def __init__(self, project_name: str) -> None:
        self.project_name: Final = project_name

    @overload
    def __call__(
        self,
        function: _F,
        firstresult: bool = False,
        historic: bool = False,
        warn_on_impl: Warning | None = None,
        warn_on_impl_args: Mapping[str, Warning] | None = None,
    ) -> _F: ...

    @overload
    def __call__(
        self,
        function: None = ...,
        firstresult: bool = ...,
        historic: bool = ...,
        warn_on_impl: Warning | None = ...,
        warn_on_impl_args: Mapping[str, Warning] | None = ...,
    ) -> Callable[[_F], _F]: ...

    def __call__(
        self,
        function: _F | None = None,
        firstresult: bool = False,
        historic: bool = False,
        warn_on_impl: Warning | None = None,
        warn_on_impl_args: Mapping[str, Warning] | None = None,
    ) -> _F | Callable[[_F], _F]:
        """If passed a function, directly sets attributes on the function
        which will make it discoverable to :meth:`PluginManager.add_hookspecs`.

        If passed no function, returns a decorator which can be applied to a
        function later using the attributes supplied.

        :param firstresult:
            If ``True``, the 1:N hook call (N being the number of registered
            hook implementation functions) will stop at I<=N when the I'th
            function returns a non-``None`` result. See :ref:`firstresult`.

        :param historic:
            If ``True``, every call to the hook will be memorized and replayed
            on plugins registered after the call was made. See :ref:`historic`.

        :param warn_on_impl:
            If given, every implementation of this hook will trigger the given
            warning. See :ref:`warn_on_impl`.

        :param warn_on_impl_args:
            If given, every implementation of this hook which requests one of
            the arguments in the dict will trigger the corresponding warning.
            See :ref:`warn_on_impl`.

            .. versionadded:: 1.5
        """

        def setattr_hookspec_opts(func: _F) -> _F:
            if historic and firstresult:
                raise ValueError("cannot have a historic firstresult hook")
            opts: HookspecOpts = {
                "firstresult": firstresult,
                "historic": historic,
                "warn_on_impl": warn_on_impl,
                "warn_on_impl_args": warn_on_impl_args,
            }
            setattr(func, self.project_name + "_spec", opts)
            return func

        if function is not None:
            return setattr_hookspec_opts(function)
        else:
            return setattr_hookspec_opts


@final
class HookimplMarker:
    """Decorator for marking functions as hook implementations.

    Instantiate it with a ``project_name`` to get a decorator.
    Calling :meth:`PluginManager.register` later will discover all marked
    functions if the :class:`PluginManager` uses the same project name.
    """

    __slots__ = ("project_name",)

    def __init__(self, project_name: str) -> None:
        self.project_name: Final = project_name

    @overload
    def __call__(
        self,
        function: _F,
        hookwrapper: bool = ...,
        optionalhook: bool = ...,
        tryfirst: bool = ...,
        trylast: bool = ...,
        specname: str | None = ...,
        wrapper: bool = ...,
    ) -> _F: ...

    @overload
    def __call__(
        self,
        function: None = ...,
        hookwrapper: bool = ...,
        optionalhook: bool = ...,
        tryfirst: bool = ...,
        trylast: bool = ...,
        specname: str | None = ...,
        wrapper: bool = ...,
    ) -> Callable[[_F], _F]: ...

    def __call__(
        self,
        function: _F | None = None,
        hookwrapper: bool = False,
        optionalhook: bool = False,
        tryfirst: bool = False,
        trylast: bool = False,
        specname: str | None = None,
        wrapper: bool = False,
    ) -> _F | Callable[[_F], _F]:
        """If passed a function, directly sets attributes on the function
        which will make it discoverable to :meth:`PluginManager.register`.

        If passed no function, returns a decorator which can be applied to a
        function later using the attributes supplied.

        :param optionalhook:
            If ``True``, a missing matching hook specification will not result
            in an error (by default it is an error if no matching spec is
            found). See :ref:`optionalhook`.

        :param tryfirst:
            If ``True``, this hook implementation will run as early as possible
            in the chain of N hook implementations for a specification. See
            :ref:`callorder`.

        :param trylast:
            If ``True``, this hook implementation will run as late as possible
            in the chain of N hook implementations for a specification. See
            :ref:`callorder`.

        :param wrapper:
            If ``True`` ("new-style hook wrapper"), the hook implementation
            needs to execute exactly one ``yield``. The code before the
            ``yield`` is run early before any non-hook-wrapper function is run.
            The code after the ``yield`` is run after all non-hook-wrapper
            functions have run. The ``yield`` receives the result value of the
            inner calls, or raises the exception of inner calls (including
            earlier hook wrapper calls). The return value of the function
            becomes the return value of the hook, and a raised exception becomes
            the exception of the hook. See :ref:`hookwrapper`.

        :param hookwrapper:
            If ``True`` ("old-style hook wrapper"), the hook implementation
            needs to execute exactly one ``yield``. The code before the
            ``yield`` is run early before any non-hook-wrapper function is run.
            The code after the ``yield`` is run after all non-hook-wrapper
            function have run  The ``yield`` receives a :class:`Result` object
            representing the exception or result outcome of the inner calls
            (including earlier hook wrapper calls). This option is mutually
            exclusive with ``wrapper``. See :ref:`old_style_hookwrapper`.

        :param specname:
            If provided, the given name will be used instead of the function
            name when matching this hook implementation to a hook specification
            during registration. See :ref:`specname`.

        .. versionadded:: 1.2.0
            The ``wrapper`` parameter.
        """

        def setattr_hookimpl_opts(func: _F) -> _F:
            opts: HookimplOpts = {
                "wrapper": wrapper,
                "hookwrapper": hookwrapper,
                "optionalhook": optionalhook,
                "tryfirst": tryfirst,
                "trylast": trylast,
                "specname": specname,
            }
            setattr(func, self.project_name + "_impl", opts)
            return func

        if function is None:
            return setattr_hookimpl_opts
        else:
            return setattr_hookimpl_opts(function)


_PYPY = sys.implementation.name == "pypy"
_IMPLICIT_NAMES = ("self", "cls", "obj") if _PYPY else ("self", "cls")

# Qualnames whose missing-self deprecation warning is suppressed because
# their upstream code is already fixed but not yet released.
# Remove entries once a release with the fix is available.
_NOSELF_WARN_SUPPRESS: frozenset[str] = frozenset(
    {
        # pytest-timeout >=2.3.2 has the fix, but is unreleased as of 2026-05.
        "TimeoutHooks.pytest_timeout_set_timer",
        "TimeoutHooks.pytest_timeout_cancel_timer",
    }
)

_CO_VARKEYWORDS = inspect.CO_VARKEYWORDS


def hook_parameters(
    func: object, *, legacy_noself: bool = False
) -> HookParameters:
    """Return bindable hook parameters without resolving annotations."""
    is_bound = False
    if inspect.isclass(func):
        try:
            func = func.__init__
        except AttributeError:  # pragma: no cover - pypy special case
            func = None
        is_bound = True
    elif not inspect.isroutine(func):
        try:
            func = getattr(func, "__call__", func)  # noqa: B004
        except Exception:  # pragma: no cover - pypy special case
            func = None
    if func is not None:
        if inspect.ismethod(func):
            is_bound = True
        unwrapped = inspect.unwrap(func)
        if inspect.ismethod(unwrapped):
            is_bound = True
            unwrapped = unwrapped.__func__
        func = unwrapped

    if func is None:
        return HookParameters((), (), (), (), (), {}, {}, False)

    try:
        code = func.__code__
        defaults = func.__defaults__
        kwdefaults = func.__kwdefaults__
        qualname = func.__qualname__
        if not isinstance(code, types.CodeType):
            raise AttributeError("hook function without a code object")
    except AttributeError:  # pragma: no cover
        return HookParameters((), (), (), (), (), {}, {}, False)

    positional = tuple(code.co_varnames[: code.co_argcount])
    tail = qualname.rsplit("<locals>.", maxsplit=1)[-1]
    is_class_method = "." in tail
    if positional:
        if is_bound or (is_class_method and positional[0] in _IMPLICIT_NAMES):
            positional = positional[1:]
        elif (
            is_class_method
            and legacy_noself
            and tail not in _NOSELF_WARN_SUPPRESS
        ):
            warnings.warn(
                f"{qualname} is a method but its first parameter"
                f" {positional[0]!r} is not 'self'."
                f" Add 'self' as the first parameter or use @staticmethod."
                f" This will become an error in a future version of pluggy.",
                DeprecationWarning,
                stacklevel=3,
            )

    positional_only_count = code.co_posonlyargcount
    positional_only = positional[:positional_only_count]

    if defaults:
        default_index = -len(defaults)
        positional_required = positional[:default_index]
        positional_optional = positional[default_index:]
        positional_defaults = dict(zip(positional_optional, defaults))
    else:
        positional_required = positional
        positional_optional = ()
        positional_defaults = {}

    kwonly = tuple(
        code.co_varnames[
            code.co_argcount : code.co_argcount + code.co_kwonlyargcount
        ]
    )
    kwonly_defaults = kwdefaults or {}
    kwonly_required = tuple(name for name in kwonly if name not in kwonly_defaults)
    kwonly_optional = tuple(name for name in kwonly if name in kwonly_defaults)

    return HookParameters(
        positional_required,
        positional_optional,
        positional_only,
        kwonly_required,
        kwonly_optional,
        positional_defaults,
        kwonly_defaults,
        bool(code.co_flags & _CO_VARKEYWORDS),
    )


def varnames(
    func: object, *, legacy_noself: bool = False
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return tuple of positional and keyword parameter names for a callable.

    In case of a class, its ``__init__`` method is considered.
    For bound methods, the already-bound first parameter is not included.
    For unbound methods with a dotted ``__qualname__``, the first parameter is
    stripped only if its name is a known implicit name (``self``, ``cls``).
    Keyword-only parameters are not included.

    :param legacy_noself:
        If ``True``, support hookspec classes whose methods omit ``self``.
        When the function looks like a class method but has no implicit first
        parameter, a :class:`DeprecationWarning` is emitted.
    """
    parameters = hook_parameters(func, legacy_noself=legacy_noself)
    return (
        parameters.positional_argnames,
        parameters.positional_kwargnames,
    )


@final
class HookSpec:
    __slots__ = (
        "argnames",
        "function",
        "kwonly_argnames",
        "kwonly_kwargnames",
        "kwargdefaults",
        "kwargnames",
        "name",
        "namespace",
        "opts",
        "parameters",
        "positional_argnames",
        "var_keyword",
        "warn_on_impl",
        "warn_on_impl_args",
    )

    def __init__(self, namespace: _Namespace, name: str, opts: HookspecOpts) -> None:
        self.namespace = namespace
        self.name = name
        self.function: Callable[..., object] = getattr(namespace, name)
        legacy_noself = inspect.isclass(namespace) and not isinstance(
            inspect.getattr_static(namespace, name), staticmethod
        )
        self.parameters = hook_parameters(
            self.function, legacy_noself=legacy_noself
        )
        self.argnames = self.parameters.argnames
        self.kwargnames = self.parameters.kwargnames
        self.positional_argnames = (
            self.parameters.positional_argnames
            + self.parameters.positional_kwargnames
        )
        self.kwonly_argnames = self.parameters.kwonly_argnames
        self.kwonly_kwargnames = self.parameters.kwonly_kwargnames
        self.kwargdefaults = {
            **self.parameters.argdefaults,
            **self.parameters.kwonly_argdefaults,
        }
        self.var_keyword = self.parameters.var_keyword
        if self.parameters.positional_only_argnames:
            raise ValueError(
                "Positional-only arguments are not supported in hook "
                "specifications because hook arguments are bound by name"
            )
        self.opts = opts
        self.warn_on_impl = opts.get("warn_on_impl")
        self.warn_on_impl_args = opts.get("warn_on_impl_args")
