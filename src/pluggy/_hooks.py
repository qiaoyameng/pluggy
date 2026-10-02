"""
Internal hook annotation, representation and calling machinery.

This module re-exports symbols from the role-specific modules for
backward compatibility.
"""

from __future__ import annotations

from ._caller import _HookCaller
from ._caller import _HookExec
from ._caller import _HookRelay
from ._caller import _SubsetHookCaller
from ._caller import HookCaller
from ._caller import HookRelay
from ._config import HookimplOpts
from ._config import HookspecOpts
from ._config import normalize_hookimpl_opts
from ._decorators import _Namespace
from ._decorators import HookimplMarker
from ._decorators import HookSpec
from ._decorators import HookspecMarker
from ._decorators import varnames
from ._impl import _HookImplFunction
from ._impl import _Plugin
from ._impl import HookImpl
from ._observability import HookImplObserver
from ._observability import HookCallEvent
from ._observability import HookCallResultEvent
from ._observability import ImplEndEvent
from ._observability import ImplExceptionEvent
from ._observability import ImplStartEvent
from ._observability import PHASE_IMPL
from ._observability import PHASE_WRAPPER_ENTER
from ._observability import PHASE_WRAPPER_EXIT
from ._observability import PluggyObserverWarning


__all__ = [
    "HookCaller",
    "HookImpl",
    "HookRelay",
    "HookSpec",
    "HookimplMarker",
    "HookimplOpts",
    "HookCallEvent",
    "HookCallResultEvent",
    "HookImplObserver",
    "HookspecMarker",
    "HookspecOpts",
    "ImplEndEvent",
    "ImplExceptionEvent",
    "ImplStartEvent",
    "PHASE_IMPL",
    "PHASE_WRAPPER_ENTER",
    "PHASE_WRAPPER_EXIT",
    "PluggyObserverWarning",
    "_HookCaller",
    "_HookExec",
    "_HookImplFunction",
    "_HookRelay",
    "_Namespace",
    "_Plugin",
    "_SubsetHookCaller",
    "normalize_hookimpl_opts",
    "varnames",
]
