"""
Hook implementation representation.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Generator
from typing import Final
from typing import final
from typing import TypeAlias
from typing import TypeVar

from ._config import HookimplOpts
from ._decorators import hook_parameters
from ._result import Result


_T = TypeVar("_T")

_Plugin: TypeAlias = object
_HookImplFunction: TypeAlias = Callable[..., _T | Generator[None, Result[_T], None]]


@final
class HookImpl:
    """A hook implementation in a :class:`HookCaller`."""

    __slots__ = (
        "argnames",
        "function",
        "hookwrapper",
        "kwonly_argnames",
        "kwonly_kwargnames",
        "kwargnames",
        "optionalhook",
        "opts",
        "parameters",
        "plugin",
        "plugin_name",
        "positional_only_argnames",
        "positional_argnames",
        "tryfirst",
        "trylast",
        "var_keyword",
        "wrapper",
    )

    def __init__(
        self,
        plugin: _Plugin,
        plugin_name: str,
        function: _HookImplFunction[object],
        hook_impl_opts: HookimplOpts,
    ) -> None:
        """:meta private:"""
        #: The hook implementation function.
        self.function: Final = function
        parameters = hook_parameters(self.function)
        self.parameters: Final = parameters
        #: The positional parameter names of ``function``.
        self.positional_argnames: Final = (
            parameters.positional_argnames + parameters.positional_kwargnames
        )
        self.positional_only_argnames: Final = (
            parameters.positional_only_argnames
        )
        self.argnames: Final = parameters.argnames
        #: The positional-or-keyword parameters of ``function`` with defaults.
        self.kwargnames: Final = parameters.kwargnames
        self.kwonly_argnames: Final = parameters.kwonly_argnames
        self.kwonly_kwargnames: Final = parameters.kwonly_kwargnames
        self.var_keyword: Final = parameters.var_keyword
        #: The plugin which defined this hook implementation.
        self.plugin: Final = plugin
        #: The :class:`HookimplOpts` used to configure this hook implementation.
        self.opts: Final = hook_impl_opts
        #: The name of the plugin which defined this hook implementation.
        self.plugin_name: Final = plugin_name
        #: Whether the hook implementation is a :ref:`wrapper <hookwrapper>`.
        self.wrapper: Final = hook_impl_opts["wrapper"]
        #: Whether the hook implementation is an :ref:`old-style wrapper
        #: <old_style_hookwrappers>`.
        self.hookwrapper: Final = hook_impl_opts["hookwrapper"]
        #: Whether validation against a hook specification is :ref:`optional
        #: <optionalhook>`.
        self.optionalhook: Final = hook_impl_opts["optionalhook"]
        #: Whether to try to order this hook implementation :ref:`first
        #: <callorder>`.
        self.tryfirst: Final = hook_impl_opts["tryfirst"]
        #: Whether to try to order this hook implementation :ref:`last
        #: <callorder>`.
        self.trylast: Final = hook_impl_opts["trylast"]

    def __repr__(self) -> str:
        return f"<HookImpl plugin_name={self.plugin_name!r}, plugin={self.plugin!r}>"
