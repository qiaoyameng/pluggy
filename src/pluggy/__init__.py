__all__ = [
    "HookCallError",
    "HookCallEvent",
    "HookCallResultEvent",
    "HookCaller",
    "HookImpl",
    "HookImplObserver",
    "HookRelay",
    "HookimplMarker",
    "HookimplOpts",
    "HookspecMarker",
    "HookspecOpts",
    "ImplEndEvent",
    "ImplExceptionEvent",
    "ImplStartEvent",
    "PHASE_IMPL",
    "PHASE_WRAPPER_ENTER",
    "PHASE_WRAPPER_EXIT",
    "PluggyObserverWarning",
    "PluggyTeardownRaisedWarning",
    "PluggyWarning",
    "PluginManager",
    "PluginValidationError",
    "Result",
    "__version__",
]
from ._hooks import HookCaller
from ._hooks import HookCallEvent
from ._hooks import HookCallResultEvent
from ._hooks import HookImpl
from ._hooks import HookImplObserver
from ._hooks import HookimplMarker
from ._hooks import HookimplOpts
from ._hooks import HookRelay
from ._hooks import HookspecMarker
from ._hooks import HookspecOpts
from ._hooks import ImplEndEvent
from ._hooks import ImplExceptionEvent
from ._hooks import ImplStartEvent
from ._hooks import PHASE_IMPL
from ._hooks import PHASE_WRAPPER_ENTER
from ._hooks import PHASE_WRAPPER_EXIT
from ._hooks import PluggyObserverWarning
from ._manager import PluginManager
from ._manager import PluginValidationError
from ._result import HookCallError
from ._result import Result
from ._warnings import PluggyTeardownRaisedWarning
from ._warnings import PluggyWarning


def __getattr__(name: str) -> str:
    if name == "__version__":
        from importlib.metadata import version

        return version("pluggy")

    raise AttributeError(f"module {__name__} has no attribute {name!r}")
