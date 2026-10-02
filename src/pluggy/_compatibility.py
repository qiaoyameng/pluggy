"""Read-only compatibility checks between installed plugins and candidate hookspecs.

The rules here are the exact same ones enforced at registration time by
:meth:`pluggy.PluginManager.register` /
:meth:`pluggy.PluginManager.add_hookspecs` -- see
:func:`iter_hook_violations`. Nothing in this module mutates a live
:class:`~pluggy.PluginManager`: candidate specifications are only inspected,
never installed, and hook implementations are never executed.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator
import inspect
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import TypeAlias

from ._decorators import HookSpec


if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._caller import HookCaller
    from ._decorators import _Namespace
    from ._impl import HookImpl
    from ._manager import PluginManager


#: A hard failure: the plugin would no longer validate against the candidate
#: hookspec.
ERROR: TypeAlias = "error"
#: A declaration change worth reviewing, but which does not break any
#: currently installed plugin under the shared validation rules.
WARNING: TypeAlias = "warning"

Severity: TypeAlias = str


def _formatdef(func: Callable[..., object]) -> str:
    return f"{func.__name__}{inspect.signature(func)}"


@dataclass(frozen=True)
class _Violation:
    """One rule broken by an implementation against a specification.

    Kinds and messages mirror the errors historically raised by
    :meth:`PluginManager._verify_hook`, so the read-only check can never
    disagree with registration.
    """

    kind: str
    message: str


def iter_hook_violations(
    hook_name: str, spec: HookSpec, hookimpl: HookImpl
) -> Iterator[_Violation]:
    """Yield the registration-validation violations of ``hookimpl``.

    The order and wording match :meth:`PluginManager._verify_hook` exactly so
    that registration and the read-only compatibility check always reach the
    same verdict.
    """
    if spec.opts["historic"] and (hookimpl.hookwrapper or hookimpl.wrapper):
        yield _Violation(
            "historic_wrapper",
            f"Plugin {hookimpl.plugin_name!r}\nhook {hook_name!r}\n"
            "historic incompatible with yield/wrapper/hookwrapper",
        )

    notinspec = set(hookimpl.argnames) - (
        set(spec.argnames) | set(spec.kwargnames)
    )
    if notinspec:
        yield _Violation(
            "unknown_argument",
            f"Plugin {hookimpl.plugin_name!r} for hook {hook_name!r}\n"
            f"hookimpl definition: {_formatdef(hookimpl.function)}\n"
            f"Argument(s) {notinspec} are declared in the hookimpl but "
            "can not be found in the hookspec",
        )

    if (
        hookimpl.wrapper or hookimpl.hookwrapper
    ) and not inspect.isgeneratorfunction(hookimpl.function):
        yield _Violation(
            "wrapper_not_generator",
            f"Plugin {hookimpl.plugin_name!r} for hook {hook_name!r}\n"
            f"hookimpl definition: {_formatdef(hookimpl.function)}\n"
            "Declared as wrapper=True or hookwrapper=True "
            "but function is not a generator function",
        )

    if hookimpl.wrapper and hookimpl.hookwrapper:
        yield _Violation(
            "wrapper_conflict",
            f"Plugin {hookimpl.plugin_name!r} for hook {hook_name!r}\n"
            f"hookimpl definition: {_formatdef(hookimpl.function)}\n"
            "The wrapper=True and hookwrapper=True options are mutually exclusive",
        )


@dataclass(frozen=True)
class HookspecConflict:
    """A single incompatibility between installed plugins and a candidate
    hookspec declaration.

    :param hook:
        Name of the affected hook.

    :param kind:
        Machine readable conflict kind. Per-plugin kinds produced by the
        shared registration rules are ``"historic_wrapper"``,
        ``"unknown_argument"``, ``"wrapper_not_generator"`` and
        ``"wrapper_conflict"``; declaration-level kinds are
        ``"argument_removed"``, ``"argument_keyword_only"``,
        ``"argument_added_required"``, ``"argument_added_optional"``,
        ``"argument_became_required"``, ``"argument_became_optional"``,
        and ``"historic_changed"``.

    :param message:
        Human readable description of the conflict.

    :param severity:
        ``"error"`` when an installed plugin would fail validation or change
        protocol under the candidate declaration, ``"warning"`` for a
        declaration change that does not break any installed plugin.

    :param plugin:
        The offending plugin object, or ``None`` for a declaration-level
        change that is not tied to a single plugin.

    :param plugin_name:
        Name the plugin is registered under, or ``None``.

    :param arguments:
        Hook argument names involved in the conflict, when applicable.
    """

    hook: str
    kind: str
    message: str
    severity: Severity = ERROR
    plugin: object | None = None
    plugin_name: str | None = None
    arguments: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class HookspecCompatibility:
    """Result of a read-only candidate hookspec compatibility check.

    Truthy when no installed plugin would break (i.e. there are no
    :attr:`~HookspecConflict.severity` ``"error"`` conflicts), even if
    declaration-level warnings remain.
    """

    conflicts: tuple[HookspecConflict, ...]

    @property
    def errors(self) -> tuple[HookspecConflict, ...]:
        """Conflicts which would prevent an installed plugin from working."""
        return tuple(c for c in self.conflicts if c.severity == ERROR)

    @property
    def warnings(self) -> tuple[HookspecConflict, ...]:
        """Declaration changes worth reviewing but breaking no plugin."""
        return tuple(c for c in self.conflicts if c.severity == WARNING)

    @property
    def is_compatible(self) -> bool:
        """Whether no installed plugin breaks under the candidate hookspec."""
        return not self.errors

    def __bool__(self) -> bool:
        return self.is_compatible

    def __str__(self) -> str:
        if not self.conflicts:
            return "candidate hookspec is compatible with all installed plugins"
        lines = []
        for conflict in self.conflicts:
            if conflict.plugin_name is None:
                lines.append(
                    f"{conflict.hook!r}: [{conflict.severity}] {conflict.message}"
                )
            else:
                lines.append(
                    f"{conflict.hook!r}: [{conflict.severity}] "
                    f"plugin {conflict.plugin_name!r}: {conflict.message}"
                )
        return "\n".join(lines)


def _signature_kwargs(
    spec: HookSpec,
) -> dict[str, inspect.Parameter]:
    """Map the bindable-by-name parameters of a hookspec.

    Mirrors the set ``_verify_hook`` accepts (positional and positional/keyword
    parameters), and additionally exposes keyword-only parameters so their
    positional restriction can be diagnosed separately.
    """
    function = inspect.unwrap(spec.function)
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return {}
    kinds = (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )
    params = {
        name: param
        for name, param in parameters.items()
        if param.kind in kinds
    }
    # Drop the implicit instance/class argument of hookspec class methods.
    if params and inspect.isclass(spec.namespace):
        static = inspect.getattr_static(spec.namespace, spec.name, None)
        if not isinstance(static, staticmethod):
            first = next(iter(params.values()))
            if first.name in ("self", "cls"):
                params.pop(first.name)
    return params


def _argument_conflicts(
    hook_name: str, old: HookSpec | None, new: HookSpec
) -> Iterator[HookspecConflict]:
    """Diagnose declaration-level argument changes between two spec versions."""
    old_params = _signature_kwargs(old) if old is not None else {}
    new_params = _signature_kwargs(new)
    # Names the call machinery passes positionally/by-name under each version.
    if old is not None:
        old_bindable = set(old.argnames) | set(old.kwargnames)
    else:
        old_bindable = set()
    new_bindable = set(new.argnames) | set(new.kwargnames)

    for name in old_bindable - new_bindable:
        param = new_params.get(name)
        if param is not None and param.kind == inspect.Parameter.KEYWORD_ONLY:
            kind, detail = (
                "argument_keyword_only",
                "becomes keyword-only in the candidate hookspec; "
                "implementations requesting it positionally can no longer "
                "validate",
            )
        else:
            kind, detail = (
                "argument_removed",
                "is removed from the candidate hookspec; implementations "
                "requesting it can no longer validate",
            )
        yield HookspecConflict(
            hook=hook_name,
            kind=kind,
            severity=WARNING,
            arguments=frozenset({name}),
            message=f"argument {name!r} {detail}",
        )

    for name in old_bindable & new_bindable:
        old_param = old_params.get(name)
        new_param = new_params.get(name)
        if (
            old_param is not None
            and new_param is not None
            and old_param.kind != inspect.Parameter.KEYWORD_ONLY
            and new_param.kind == inspect.Parameter.KEYWORD_ONLY
        ):
            yield HookspecConflict(
                hook=hook_name,
                kind="argument_keyword_only",
                severity=WARNING,
                arguments=frozenset({name}),
                message=(
                    f"argument {name!r} becomes keyword-only in the candidate "
                    "hookspec; implementations requesting it positionally can "
                    "no longer validate"
                ),
            )
        if old_param is None or new_param is None:
            continue
        required = new_param.default is inspect.Parameter.empty
        was_required = old_param.default is inspect.Parameter.empty
        if required and not was_required:
            yield HookspecConflict(
                hook=hook_name,
                kind="argument_became_required",
                severity=WARNING,
                arguments=frozenset({name}),
                message=(
                    f"argument {name!r} becomes required in the candidate "
                    "hookspec (it had a default)"
                ),
            )
        elif not required and was_required:
            yield HookspecConflict(
                hook=hook_name,
                kind="argument_became_optional",
                severity=WARNING,
                arguments=frozenset({name}),
                message=(
                    f"argument {name!r} gains a default in the candidate "
                    "hookspec (it was required)"
                ),
            )

    for name, param in new_params.items():
        if name in old_bindable:
            continue
        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            # Keyword-only parameters are not passed by the call machinery;
            # they are irrelevant to already-registered implementations.
            continue
        required = param.default is inspect.Parameter.empty
        yield HookspecConflict(
            hook=hook_name,
            kind=(
                "argument_added_required" if required else "argument_added_optional"
            ),
            severity=WARNING,
            arguments=frozenset({name}),
            message=(
                f"argument {name!r} is added to the candidate hookspec "
                f"({'required' if required else 'optional, with a default'})"
            ),
        )


def check_compatibility(
    manager: PluginManager, module_or_class: _Namespace
) -> HookspecCompatibility:
    """Check installed plugins against candidate hookspec declarations.

    See :meth:`PluginManager.check_hookspec_compatibility` for the public
    contract. Implemented here (next to the shared validation rules) so the
    check and registration can never derive opposite conclusions.
    """
    conflicts: list[HookspecConflict] = []
    # Inspecting a candidate hookspec can trigger the same deprecation
    # warnings a real ``add_hookspecs`` would; the check is read-only and must
    # stay silent, so callers do not mistake it for a state change.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        candidate_specs = _collect_candidate_specs(manager, module_or_class)
        # Building the HookSpec introspects ``varnames`` and may itself warn
        # (e.g. the legacy missing-self deprecation); keep that silent too.
        candidates = {
            hook_name: HookSpec(module_or_class, hook_name, spec_opts)
            for hook_name, spec_opts in candidate_specs
        }

    if not candidate_specs:
        # Same rule ``add_hookspecs`` applies; raised before anything is
        # inspected and without touching the manager.
        raise ValueError(
            f"did not find any {manager.project_name!r} hooks in "
            f"{module_or_class!r}"
        )

    for hook_name, spec_opts in candidate_specs:
        existing: HookCaller | None = getattr(manager.hook, hook_name, None)
        old_spec = existing.spec if existing is not None else None
        candidate = candidates[hook_name]

        conflicts.extend(_argument_conflicts(hook_name, old_spec, candidate))

        if old_spec is not None and (
            old_spec.opts["firstresult"] != spec_opts["firstresult"]
        ):
            conflicts.append(
                HookspecConflict(
                    hook=hook_name,
                    kind="firstresult_changed",
                    severity=WARNING,
                    message=(
                        "firstresult changes from "
                        f"{old_spec.opts['firstresult']!r} to "
                        f"{spec_opts['firstresult']!r} in the candidate "
                        "hookspec; the hook's result protocol changes for all "
                        "implementations"
                    ),
                )
            )

        if old_spec is not None and (
            old_spec.opts["historic"] != spec_opts["historic"]
        ):
            conflicts.append(
                HookspecConflict(
                    hook=hook_name,
                    kind="historic_changed",
                    severity=WARNING,
                    message=(
                        "historic changes from "
                        f"{old_spec.opts['historic']!r} to "
                        f"{spec_opts['historic']!r} in the candidate hookspec"
                    ),
                )
            )

        if existing is None:
            continue

        hookimpls: Sequence[HookImpl] = existing.get_hookimpls()
        baseline = {
            impl: {v.kind for v in iter_hook_violations(hook_name, old_spec, impl)}
            for impl in hookimpls
        } if old_spec is not None else {}

        for impl in hookimpls:
            for violation in iter_hook_violations(hook_name, candidate, impl):
                if violation.kind in baseline.get(impl, set()):
                    continue
                conflicts.append(
                    HookspecConflict(
                        hook=hook_name,
                        kind=violation.kind,
                        message=violation.message,
                        severity=ERROR,
                        plugin=impl.plugin,
                        plugin_name=impl.plugin_name,
                    )
                )

            if (
                old_spec is not None
                and (impl.wrapper or impl.hookwrapper)
                and old_spec.opts["firstresult"] != spec_opts["firstresult"]
            ):
                conflicts.append(
                    HookspecConflict(
                        hook=hook_name,
                        kind="firstresult_changed",
                        severity=ERROR,
                        plugin=impl.plugin,
                        plugin_name=impl.plugin_name,
                        message=(
                            "wrapper implementation participates in a hook "
                            "whose firstresult protocol changes from "
                            f"{old_spec.opts['firstresult']!r} to "
                            f"{spec_opts['firstresult']!r}; the value received "
                            "at its yield changes shape"
                        ),
                    )
                )

    return HookspecCompatibility(tuple(conflicts))


def _collect_candidate_specs(
    manager: PluginManager, module_or_class: _Namespace
) -> list[tuple[str, dict[str, object]]]:
    """Discover candidate spec options exactly like ``add_hookspecs``."""
    specs: list[tuple[str, dict[str, object]]] = []
    for name in dir(module_or_class):
        spec_opts = manager.parse_hookspec_opts(module_or_class, name)
        if spec_opts is not None:
            specs.append((name, spec_opts))
    return specs
