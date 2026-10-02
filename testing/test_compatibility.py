"""Tests for the read-only candidate hookspec compatibility check."""

from __future__ import annotations

import types

from pluggy import HookimplMarker
from pluggy import HookspecCompatibility
from pluggy import HookspecConflict
from pluggy import HookspecMarker
from pluggy import PluginManager


hookspec = HookspecMarker("example")
hookimpl = HookimplMarker("example")


class CurrentSpec:
    @hookspec
    def setup(self, config, verbose=False): ...

    @hookspec
    def teardown(self, thing): ...

    @hookspec(firstresult=True)
    def resolve(self, name): ...

    @hookspec
    def configure(self, options): ...


class GoodPlugin:
    @hookimpl
    def setup(self, config):
        return ("setup", config)

    @hookimpl
    def teardown(self, thing):
        return ("teardown", thing)

    @hookimpl
    def resolve(self, name):
        return ("resolve", name)

    @hookimpl
    def configure(self, options):
        return ("configure", options)


class ResolveWrapper:
    @hookimpl(wrapper=True)
    def resolve(self, name):
        return (yield)


class OldStyleResolveWrapper:
    @hookimpl(hookwrapper=True)
    def resolve(self, name):
        outcome = yield
        return outcome.get_result()


def _manager(*plugins: object) -> PluginManager:
    pm = PluginManager("example")
    pm.add_hookspecs(CurrentSpec)
    for plugin in plugins:
        pm.register(plugin)
    return pm


def _find(
    result: HookspecCompatibility, kind: str, **attrs: object
) -> HookspecConflict:
    matches = [
        c
        for c in result.conflicts
        if c.kind == kind and all(getattr(c, key) == val for key, val in attrs.items())
    ]
    assert matches, f"no conflict kind={kind!r} attrs={attrs!r} in {result.conflicts}"
    return matches[0]


def test_compatible_candidate_reports_no_errors() -> None:
    pm = _manager(GoodPlugin(), ResolveWrapper())

    class Candidate:
        @hookspec
        def setup(self, config, verbose=False, mode="auto"): ...

        @hookspec
        def teardown(self, thing): ...

        @hookspec(firstresult=True)
        def resolve(self, name): ...

        @hookspec
        def configure(self, options): ...

    result = pm.check_hookspec_compatibility(Candidate)

    assert result.is_compatible
    assert bool(result) is True
    assert result.errors == ()
    assert _find(result, "argument_added_optional").arguments == frozenset({"mode"})
    # Original hooks still callable exactly as before.
    assert pm.hook.setup(config="cfg") == [("setup", "cfg")]
    assert pm.hook.resolve(name="x") == ("resolve", "x")
    assert pm.hook.teardown(thing="t") == [("teardown", "t")]


def test_removed_argument_conflicts_for_implementations() -> None:
    class UsesExtra:
        @hookimpl
        def configure(self, options, extra):
            return extra

    pm = PluginManager("example")

    class LegacySpec:
        @hookspec
        def configure(self, options, extra): ...

    pm.add_hookspecs(LegacySpec)
    pm.register(UsesExtra(), name="uses_extra")

    class Candidate:
        @hookspec
        def configure(self, options): ...

    result = pm.check_hookspec_compatibility(Candidate)

    assert not result.is_compatible
    error = _find(result, "unknown_argument", plugin_name="uses_extra")
    assert error.severity == "error"
    assert "extra" in error.message
    warning = _find(result, "argument_removed")
    assert warning.arguments == frozenset({"extra"})
    assert warning.plugin is None


def test_added_required_argument_is_only_a_warning() -> None:
    pm = _manager(GoodPlugin())

    class Candidate:
        @hookspec
        def setup(self, config, mode, verbose=False): ...

        @hookspec
        def teardown(self, thing): ...

        @hookspec(firstresult=True)
        def resolve(self, name): ...

        @hookspec
        def configure(self, options): ...

    result = pm.check_hookspec_compatibility(Candidate)

    # Existing impls declare a subset of the spec args, so this is the same
    # conclusion registration reaches: no plugin breaks.
    assert result.is_compatible
    added = _find(result, "argument_added_required")
    assert added.arguments == frozenset({"mode"})
    assert added.severity == "warning"


def test_required_optional_changes_are_diagnosed() -> None:
    pm = _manager(GoodPlugin())

    class Candidate:
        @hookspec
        def setup(self, config, verbose): ...  # optional -> required

        @hookspec
        def teardown(self, thing=None): ...  # required -> optional

        @hookspec(firstresult=True)
        def resolve(self, name): ...

        @hookspec
        def configure(self, options): ...

    result = pm.check_hookspec_compatibility(Candidate)

    assert result.is_compatible  # neither breaks an installed plugin
    assert _find(result, "argument_became_required").arguments == frozenset(
        {"verbose"}
    )
    assert _find(result, "argument_became_optional").arguments == frozenset(
        {"thing"}
    )


def test_positional_restriction_keyword_only_argument() -> None:
    class RequestsVerbose:
        @hookimpl
        def setup(self, config, verbose):
            return verbose

    pm = _manager(RequestsVerbose())

    class Candidate:
        @hookspec
        def setup(self, config, *, verbose=False): ...

        @hookspec
        def teardown(self, thing): ...

        @hookspec(firstresult=True)
        def resolve(self, name): ...

        @hookspec
        def configure(self, options): ...

    result = pm.check_hookspec_compatibility(Candidate)

    assert not result.is_compatible
    _find(result, "argument_keyword_only", arguments=frozenset({"verbose"}))
    _find(result, "unknown_argument", hook="setup")


def test_firstresult_change_is_per_wrapper_conflict() -> None:
    wrapper = ResolveWrapper()
    pm = _manager(GoodPlugin(), wrapper)
    wrapper_name = pm.get_name(wrapper)

    class Candidate:
        @hookspec
        def setup(self, config, verbose=False): ...

        @hookspec
        def teardown(self, thing): ...

        @hookspec  # firstresult dropped
        def resolve(self, name): ...

        @hookspec
        def configure(self, options): ...

    result = pm.check_hookspec_compatibility(Candidate)

    assert not result.is_compatible
    declaration = _find(
        result, "firstresult_changed", hook="resolve", severity="warning"
    )
    assert "True" in declaration.message and "False" in declaration.message
    per_plugin = [
        c
        for c in result.errors
        if c.kind == "firstresult_changed" and c.plugin_name is not None
    ]
    assert per_plugin, result
    assert [(c.plugin_name, c.plugin) for c in per_plugin] == [
        (wrapper_name, wrapper)
    ]


def test_firstresult_change_old_style_wrapper_conflict() -> None:
    wrapper = OldStyleResolveWrapper()
    pm = _manager(wrapper)
    wrapper_name = pm.get_name(wrapper)

    class Candidate:
        @hookspec
        def setup(self, config, verbose=False): ...

        @hookspec
        def teardown(self, thing): ...

        @hookspec  # firstresult dropped
        def resolve(self, name): ...

        @hookspec
        def configure(self, options): ...

    result = pm.check_hookspec_compatibility(Candidate)
    assert any(
        c.kind == "firstresult_changed"
        and c.plugin_name == wrapper_name
        for c in result.errors
    )


def test_historic_candidate_rejects_wrappers_like_registration() -> None:
    pm = _manager(ResolveWrapper())

    class Candidate:
        @hookspec
        def setup(self, config, verbose=False): ...

        @hookspec
        def teardown(self, thing): ...

        @hookspec(firstresult=True)
        def resolve(self, name): ...

        @hookspec(historic=True)  # newly historic
        def configure(self, options): ...

    result = pm.check_hookspec_compatibility(Candidate)

    # ResolveWrapper implements ``resolve`` (still non-historic), so nothing;
    # no wrapper implements configure. Now add a configure wrapper and recheck.
    class ConfigureWrapper:
        @hookimpl(wrapper=True)
        def configure(self, options):
            return (yield)

    pm.register(ConfigureWrapper(), name="cfg_wrap")
    result = pm.check_hookspec_compatibility(Candidate)

    error = _find(result, "historic_wrapper", hook="configure")
    assert error.plugin_name == "cfg_wrap"
    assert not result.is_compatible
    # Declaration-level change is reported as well.
    _find(result, "historic_changed", hook="configure")


def test_wrapper_protocol_violations_match_registration_rules() -> None:
    class BadWrapper:
        @hookimpl(wrapper=True)
        def resolve(self, name):
            return None  # not a generator -> invalid at registration

    pm2 = PluginManager("example")
    bad = BadWrapper()
    pm2.register(bad, name="bad_wrapper")

    class Candidate:
        @hookspec
        def resolve(self, name): ...

    result = pm2.check_hookspec_compatibility(Candidate)
    _find(result, "wrapper_not_generator", plugin=bad)
    assert not result.is_compatible


def test_candidate_check_does_not_mutate_manager() -> None:
    pm = _manager(GoodPlugin(), ResolveWrapper())
    before = {
        name: caller.get_hookimpls()
        for name, caller in vars(pm.hook).items()
    }
    firstresult_before = pm.hook.resolve.spec.opts["firstresult"]
    argnames_before = tuple(pm.hook.teardown.spec.argnames)

    class Candidate:
        @hookspec
        def teardown(self, thing, mode): ...

        @hookspec
        def resolve(self, name): ...

    result = pm.check_hookspec_compatibility(Candidate)
    assert not result.is_compatible or result.warnings

    assert pm.hook.resolve.spec.opts["firstresult"] is firstresult_before
    assert tuple(pm.hook.teardown.spec.argnames) == argnames_before
    assert not hasattr(pm.hook, "brand_new_hook")
    after = {
        name: caller.get_hookimpls()
        for name, caller in vars(pm.hook).items()
    }
    assert after.keys() == before.keys()
    for name in before:
        assert after[name] == before[name]


def test_check_runs_no_implementations() -> None:
    calls: list[str] = []

    class Instrumented:
        @hookimpl
        def setup(self, config):
            calls.append("setup")

    pm = _manager(Instrumented())

    class Candidate:
        @hookspec
        def setup(self, config, removed_marker=False): ...

        @hookspec
        def teardown(self, thing): ...

        @hookspec(firstresult=True)
        def resolve(self, name): ...

        @hookspec
        def configure(self, options): ...

    pm.check_hookspec_compatibility(Candidate)
    assert calls == []


def test_check_accepts_module_and_instance() -> None:
    pm = _manager(GoodPlugin())

    module = types.ModuleType("candidate_specs")
    module.example_spec = hookspec

    def setup(config, verbose=False): ...  # pragma: no cover
    def teardown(thing): ...  # pragma: no cover
    def resolve(name): ...  # pragma: no cover
    def configure(options): ...  # pragma: no cover

    module.setup = hookspec(setup)
    module.teardown = hookspec(teardown)
    module.resolve = hookspec(firstresult=True)(resolve)
    module.configure = hookspec(configure)

    assert pm.check_hookspec_compatibility(module).is_compatible

    instance = CurrentSpec()
    assert pm.check_hookspec_compatibility(instance).is_compatible


def test_check_is_silent_even_for_legacy_noself_specs(recwarn) -> None:
    pm = _manager(GoodPlugin())

    class Candidate:
        # Methods of a hookspec class omitting ``self`` trigger a
        # deprecation when the spec is introspected; a read-only check must
        # surface no such warning.
        @hookspec
        def setup(config, verbose=False): ...  # noqa: N805

        @hookspec
        def teardown(thing): ...

        @hookspec(firstresult=True)
        def resolve(name): ...

        @hookspec
        def configure(options): ...

    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = pm.check_hookspec_compatibility(Candidate)

    assert result.is_compatible
    assert caught == []


def test_pre_existing_violation_is_not_reported() -> None:
    """A rule already broken against the installed spec is filtered out, while
    newly introduced conflicts are still reported."""

    class OldSpec:
        @hookspec
        def configure(self, options, ghost, extra): ...

    class AlreadyBad:
        @hookimpl
        def configure(self, options, ghost):
            return ghost

    pm = PluginManager("example")
    pm.add_hookspecs(OldSpec)
    bad = AlreadyBad()
    pm.register(bad, name="already_bad")

    # This plugin is valid under both old and candidate specs.
    class UsesExtra:
        @hookimpl
        def configure(self, options, ghost, extra):
            return extra

    pm.register(UsesExtra(), name="uses_extra")

    class Candidate:
        @hookspec
        def configure(self, options, ghost): ...

    # ``already_bad`` fails the shared rules both now and under the candidate,
    # so it must not be reported; ``uses_extra`` is valid now but breaks later.
    result = pm.check_hookspec_compatibility(Candidate)
    per_plugin_errors = [c for c in result.errors if c.plugin is bad]
    assert per_plugin_errors == []
    _find(
        result,
        "unknown_argument",
        plugin_name="uses_extra",
        hook="configure",
    )
    _find(result, "argument_removed", arguments=frozenset({"extra"}))


def test_empty_compatibility_result_truthiness_and_str() -> None:
    result = HookspecCompatibility(())
    assert result.is_compatible
    assert bool(result)
    assert result.errors == ()
    assert result.warnings == ()
    assert "compatible" in str(result)

    conflict = HookspecConflict(
        hook="h",
        kind="unknown_argument",
        message="boom",
        severity="error",
        plugin_name="p",
    )
    failing = HookspecCompatibility((conflict,))
    assert not failing
    assert not failing.is_compatible
    assert failing.errors == (conflict,)
    assert "'h'" in str(failing) and "p" in str(failing)


def test_candidate_without_hookspecs_raises_and_keeps_manager() -> None:
    pm = _manager(GoodPlugin())

    class NotASpec:
        def setup(self, config): ...  # pragma: no cover

    import pytest

    with pytest.raises(ValueError, match="did not find any"):
        pm.check_hookspec_compatibility(NotASpec)

    # Manager completely untouched.
    assert pm.hook.setup(config="c") == [("setup", "c")]
