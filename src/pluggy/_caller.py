"""
Hook callers and relay.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from typing import Any
from typing import Final
from typing import final
from typing import TYPE_CHECKING
from typing import TypeAlias
import warnings

from ._config import HookimplOpts
from ._config import HookspecOpts
from ._decorators import _Namespace
from ._decorators import HookSpec
from ._impl import _Plugin
from ._impl import HookImpl


_HookExec: TypeAlias = Callable[
    [str, Sequence[HookImpl], Mapping[str, object], bool],
    object | list[object],
]


@final
class HookRelay:
    """Hook holder object for performing 1:N hook calls where N is the number
    of registered plugins."""

    __slots__ = ("__dict__",)

    def __init__(self) -> None:
        """:meta private:"""

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> HookCaller: ...


# Historical name (pluggy<=1.2), kept for backward compatibility.
_HookRelay = HookRelay


_CallHistoryEntry: TypeAlias = tuple[
    int, Mapping[str, object], Callable[[Any], None] | None
]


@final
class _CallHistory:
    """The recorded history of calls to a :ref:`historic <historic>` hook.

    Every call receives a strictly increasing sequence number starting at 1.
    When the history is bounded (``size`` is not ``None``), the oldest entries
    are dropped as new ones are appended; ``dropped`` counts how many calls
    are no longer available so that late registered plugins can detect the
    gap instead of assuming they received every call.
    """

    __slots__ = ("dropped", "entries", "next_seq", "size")

    def __init__(self, size: int | None) -> None:
        self.size: Final = size
        self.entries: Final[list[_CallHistoryEntry]] = []
        self.next_seq: int = 1
        self.dropped: int = 0

    def add(
        self,
        kwargs: Mapping[str, object],
        result_callback: Callable[[Any], None] | None,
    ) -> int:
        seq = self.next_seq
        self.next_seq += 1
        self.entries.append((seq, kwargs, result_callback))
        if self.size is not None:
            evict = len(self.entries) - self.size
            if evict > 0:
                del self.entries[:evict]
                self.dropped += evict
        return seq

    def clear(self, upto: int) -> int:
        """Drop entries whose sequence number is <= ``upto``.

        Returns how many entries were dropped.
        """
        if not isinstance(upto, int) or isinstance(upto, bool):
            raise TypeError("upto must be an int")
        removed = 0
        while removed < len(self.entries) and self.entries[removed][0] <= upto:
            removed += 1
        if removed:
            del self.entries[:removed]
        self.dropped += removed
        return removed

    @property
    def start(self) -> int | None:
        """Sequence number of the oldest retained call, or ``None``."""
        return self.entries[0][0] if self.entries else None

    @property
    def end(self) -> int | None:
        """Sequence number of the newest retained call, or ``None``."""
        return self.entries[-1][0] if self.entries else None


class HookCaller:
    """A caller of all registered implementations of a hook specification."""

    __slots__ = (
        "_call_history",
        "_hookexec",
        "_hookimpls",
        "name",
        "spec",
    )

    def __init__(
        self,
        name: str,
        hook_execute: _HookExec,
        specmodule_or_class: _Namespace | None = None,
        spec_opts: HookspecOpts | None = None,
    ) -> None:
        """:meta private:"""
        #: Name of the hook getting called.
        self.name: Final = name
        self._hookexec: Final = hook_execute
        # The hookimpls list. The caller iterates it *in reverse*. Format:
        # 1. trylast nonwrappers
        # 2. nonwrappers
        # 3. tryfirst nonwrappers
        # 4. trylast wrappers
        # 5. wrappers
        # 6. tryfirst wrappers
        self._hookimpls: Final[list[HookImpl]] = []
        self._call_history: _CallHistory | None = None
        # TODO: Document, or make private.
        self.spec: HookSpec | None = None
        if specmodule_or_class is not None:
            assert spec_opts is not None
            self.set_specification(specmodule_or_class, spec_opts)

    # TODO: Document, or make private.
    def has_spec(self) -> bool:
        return self.spec is not None

    # TODO: Document, or make private.
    def set_specification(
        self,
        specmodule_or_class: _Namespace,
        spec_opts: HookspecOpts,
    ) -> None:
        if self.spec is not None:
            raise ValueError(
                f"Hook {self.spec.name!r} is already registered "
                f"within namespace {self.spec.namespace}"
            )
        self.spec = HookSpec(specmodule_or_class, self.name, spec_opts)
        if spec_opts.get("historic"):
            self._call_history = _CallHistory(spec_opts.get("history_size"))

    def is_historic(self) -> bool:
        """Whether this caller is :ref:`historic <historic>`."""
        return self._call_history is not None

    @property
    def history_start(self) -> int | None:
        """Sequence number of the oldest retained historic call.

        ``None`` when no call is currently retained. Sequence numbers start at
        1 and never decrease; a value greater than 1 (or a positive
        :attr:`history_dropped`) means earlier calls are missing.
        """
        if self._call_history is None:
            return None
        return self._call_history.start

    @property
    def history_end(self) -> int | None:
        """Sequence number of the most recent retained historic call.

        ``None`` when no call is currently retained.
        """
        if self._call_history is None:
            return None
        return self._call_history.end

    @property
    def history_dropped(self) -> int:
        """Number of historic calls no longer retained.

        A positive value means some calls happened before the currently
        retained history and will never be replayed for newly registered
        plugins.
        """
        if self._call_history is None:
            return 0
        return self._call_history.dropped

    def _remove_plugin(self, plugin: _Plugin) -> None:
        """Remove all hook implementations registered by the given plugin."""
        remaining = [impl for impl in self._hookimpls if impl.plugin != plugin]
        if len(remaining) == len(self._hookimpls):
            raise ValueError(f"plugin {plugin!r} not found")
        self._hookimpls[:] = remaining

    def get_hookimpls(self) -> list[HookImpl]:
        """Get all registered hook implementations for this hook."""
        return self._hookimpls.copy()

    def _add_hookimpl(self, hookimpl: HookImpl) -> None:
        """Add an implementation to the callback chain."""
        for i, method in enumerate(self._hookimpls):
            if method.hookwrapper or method.wrapper:
                splitpoint = i
                break
        else:
            splitpoint = len(self._hookimpls)
        if hookimpl.hookwrapper or hookimpl.wrapper:
            start, end = splitpoint, len(self._hookimpls)
        else:
            start, end = 0, splitpoint

        if hookimpl.trylast:
            self._hookimpls.insert(start, hookimpl)
        elif hookimpl.tryfirst:
            self._hookimpls.insert(end, hookimpl)
        else:
            # find last non-tryfirst method
            i = end - 1
            while i >= start and self._hookimpls[i].tryfirst:
                i -= 1
            self._hookimpls.insert(i + 1, hookimpl)

    def __repr__(self) -> str:
        return f"<HookCaller {self.name!r}>"

    def _apply_defaults(self, kwargs: Mapping[str, object]) -> Mapping[str, object]:
        if self.spec is None or not self.spec.kwargdefaults:
            return kwargs
        return {**self.spec.kwargdefaults, **kwargs}

    def _verify_all_args_are_provided(self, kwargs: Mapping[str, object]) -> None:
        # This is written to avoid expensive operations when not needed.
        if self.spec:
            for argname in self.spec.argnames:
                if argname not in kwargs:
                    notincall = ", ".join(
                        repr(argname)
                        for argname in self.spec.argnames
                        # Avoid self.spec.argnames - kwargs.keys()
                        # it doesn't preserve order.
                        if argname not in kwargs
                    )
                    warnings.warn(
                        f"Argument(s) {notincall} which are declared in the hookspec "
                        "cannot be found in this hook call",
                        # 3, not 2: the warning is raised in this helper, which
                        # is called by __call__/call_historic/call_extra, which
                        # are called by the code making the hook call.
                        stacklevel=3,
                    )
                    break

    def __call__(self, **kwargs: object) -> Any:
        """Call the hook.

        Only accepts keyword arguments, which should match the hook
        specification.

        Returns the result(s) of calling all registered plugins, see
        :ref:`calling`.
        """
        assert not self.is_historic(), (
            "Cannot directly call a historic hook - use call_historic instead."
        )
        call_kwargs = self._apply_defaults(kwargs)
        self._verify_all_args_are_provided(call_kwargs)
        firstresult = self.spec.opts.get("firstresult", False) if self.spec else False
        # Copy because plugins may register other plugins during iteration (#438).
        return self._hookexec(
            self.name, self._hookimpls.copy(), call_kwargs, firstresult
        )

    def call_historic(
        self,
        result_callback: Callable[[Any], None] | None = None,
        kwargs: Mapping[str, object] | None = None,
    ) -> int:
        """Call the hook with given ``kwargs`` for all registered plugins and
        for all plugins which will be registered afterwards, see
        :ref:`historic`.

        :param result_callback:
            If provided, will be called for each non-``None`` result obtained
            from a hook implementation.

        :returns:
            The strictly increasing sequence number assigned to this call.
            Sequence numbers start at 1 and keep increasing even when older
            calls leave the retained history, so they can be passed to
            :meth:`clear_history`.
        """
        history = self._call_history
        assert history is not None
        kwargs = kwargs or {}
        kwargs = self._apply_defaults(kwargs)
        self._verify_all_args_are_provided(kwargs)
        seq = history.add(kwargs, result_callback)
        # Historizing hooks don't return results.
        # Remember firstresult isn't compatible with historic.
        # Copy because plugins may register other plugins during iteration (#438).
        res = self._hookexec(self.name, self._hookimpls.copy(), kwargs, False)
        if result_callback is None:
            return seq
        if isinstance(res, list):
            for x in res:
                result_callback(x)
        return seq

    def clear_history(self, upto: int | None = None) -> int:
        """Drop retained historic calls.

        :param upto:
            If given, drop every retained call whose sequence number is less
            than or equal to it. Sequence numbers are the values returned by
            :meth:`call_historic`. If omitted (or ``None``), all retained
            calls are dropped.

        :returns:
            How many retained calls were dropped.

        Dropped calls (including their arguments and ``result_callback``) are
        released and will not be replayed for plugins registered afterwards.
        Newly registered plugins can detect the resulting gap through
        :attr:`history_dropped`.
        """
        history = self._call_history
        if history is None:
            assert not self.is_historic()
            return 0
        if upto is None:
            upto = history.next_seq - 1
        return history.clear(upto)

    def call_extra(
        self, methods: Sequence[Callable[..., object]], kwargs: Mapping[str, object]
    ) -> Any:
        """Call the hook with some additional temporarily participating
        methods using the specified ``kwargs`` as call parameters, see
        :ref:`call_extra`."""
        assert not self.is_historic(), (
            "Cannot directly call a historic hook - use call_historic instead."
        )
        kwargs = self._apply_defaults(kwargs)
        self._verify_all_args_are_provided(kwargs)
        opts: HookimplOpts = {
            "wrapper": False,
            "hookwrapper": False,
            "optionalhook": False,
            "trylast": False,
            "tryfirst": False,
            "specname": None,
        }
        hookimpls = self._hookimpls.copy()
        for method in methods:
            hookimpl = HookImpl(None, "<temp>", method, opts)
            # Find last non-tryfirst nonwrapper method.
            i = len(hookimpls) - 1
            while i >= 0 and (
                # Skip wrappers.
                (hookimpls[i].hookwrapper or hookimpls[i].wrapper)
                # Skip tryfirst nonwrappers.
                or hookimpls[i].tryfirst
            ):
                i -= 1
            hookimpls.insert(i + 1, hookimpl)
        firstresult = self.spec.opts.get("firstresult", False) if self.spec else False
        return self._hookexec(self.name, hookimpls, kwargs, firstresult)

    def _maybe_apply_history(self, method: HookImpl) -> None:
        """Apply call history to a new hookimpl if it is marked as historic."""
        history = self._call_history
        if history is None:
            return
        # Replay by sequence number rather than a list position. A hookimpl
        # invoked below may itself make a historic call or register another
        # plugin, which appends entries, and a bounded history may evict from
        # the front. Re-syncing after each invocation guarantees that no call
        # is skipped, replayed twice, or delivered out of order.
        last_seq = 0
        while True:
            # Retained entries always span a contiguous sequence-number
            # interval, so locate the first entry not yet delivered.
            if not history.entries:
                return
            index = last_seq + 1 - history.entries[0][0]
            if index < 0:
                # The retained history starts after an already delivered
                # entry: jump to the first retained call.
                index = 0
            elif index >= len(history.entries):
                return
            seq, kwargs, result_callback = history.entries[index]
            res = self._hookexec(self.name, [method], kwargs, False)
            if res and result_callback is not None:
                # XXX: remember firstresult isn't compat with historic
                assert isinstance(res, list)
                result_callback(res[0])
            last_seq = seq


# Historical name (pluggy<=1.2), kept for backward compatibility.
_HookCaller = HookCaller


class _SubsetHookCaller(HookCaller):
    """A proxy to another HookCaller which manages calls to all registered
    plugins except the ones from remove_plugins."""

    # This class is unusual: in inhertits from `HookCaller` so all of
    # the *code* runs in the class, but it delegates all underlying *data*
    # to the original HookCaller.
    # `subset_hook_caller` used to be implemented by creating a full-fledged
    # HookCaller, copying all hookimpls from the original. This had problems
    # with memory leaks (#346) and historic calls (#347), which make a proxy
    # approach better.
    # An alternative implementation is to use a `_getattr__`/`__getattribute__`
    # proxy, however that adds more overhead and is more tricky to implement.

    __slots__ = (
        "_orig",
        "_remove_plugins",
    )

    def __init__(self, orig: HookCaller, remove_plugins: AbstractSet[_Plugin]) -> None:
        self._orig = orig
        self._remove_plugins = remove_plugins
        self.name = orig.name  # type: ignore[misc]
        self._hookexec = orig._hookexec  # type: ignore[misc]

    @property  # type: ignore[misc]
    def _hookimpls(self) -> list[HookImpl]:
        return [
            impl
            for impl in self._orig._hookimpls
            if impl.plugin not in self._remove_plugins
        ]

    @property
    def spec(self) -> HookSpec | None:  # type: ignore[override]
        return self._orig.spec

    @property
    def _call_history(self) -> _CallHistory | None:  # type: ignore[override]
        return self._orig._call_history

    def __repr__(self) -> str:
        return f"<_SubsetHookCaller {self.name!r}>"
