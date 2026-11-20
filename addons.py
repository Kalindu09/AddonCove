# SPDX-License-Identifier: GPL-3.0-or-later
"""Discovery, state and enable/disable logic for the managed add-ons.

Blender's supported way to list add-ons is ``addon_utils.modules()`` which
covers both legacy add-ons (``bl_info``) and Extensions (``bl_ext.<repo>.<id>``),
and the supported way to switch them is ``addon_utils.enable()`` /
``addon_utils.disable()`` -- the same functions the Preferences use.
"""

import sys
import traceback
from dataclasses import dataclass

import addon_utils
import bpy

from . import sidebar, storage

# Module name of this add-on itself (e.g. "bl_ext.user_default.addoncove").
# It is never listed, so it can't be switched off from its own panel.
OWN_MODULE = __package__ or "addoncove"

STATE_ON = 'ON'
STATE_OFF = 'OFF'
STATE_ERROR = 'ERROR'  # enabled in the preferences, but failed to load


@dataclass(frozen=True, slots=True)
class AddonEntry:
    module: str
    name: str
    version: str
    author: str
    description: str
    is_extension: bool
    is_internal: bool  # Blender-internal helper add-on (hidden unless developer UI is on)


@dataclass(frozen=True, slots=True)
class ToggleResult:
    ok: bool
    message: str = ""
    warning: str = ""


# -----------------------------------------------------------------------------
# Discovery

_entries: list[AddonEntry] = []
_by_module: dict[str, AddonEntry] = {}
_signature: tuple = ()


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _version_text(value) -> str:
    if isinstance(value, (tuple, list)) and value:
        return ".".join(str(part) for part in value)
    return ""


def _build_entries(modules) -> list[AddonEntry]:
    # Private in Blender, but it is exactly what the Preferences use to hide
    # internal helper add-ons.  Fall back to an empty set if it ever goes away.
    hidden_core = set(getattr(addon_utils, "_addons_hidden_core", ()))

    entries = []
    for mod in modules:
        module = mod.__name__
        if module == OWN_MODULE:
            continue
        try:
            info = addon_utils.module_bl_info(mod)
        except Exception:
            info = getattr(mod, "bl_info", None) or {}
        entries.append(AddonEntry(
            module=module,
            name=_text(info.get("name")) or module,
            version=_version_text(info.get("version")),
            author=_text(info.get("author")),
            description=_text(info.get("description")),
            is_extension=module.startswith("bl_ext."),
            is_internal=module in hidden_core,
        ))
    entries.sort(key=lambda e: (e.name.casefold(), e.module))
    return entries


def get_entries(refresh: bool = False) -> list[AddonEntry]:
    """All installed add-ons (cached).

    ``refresh=True`` rescans the add-on folders and extension repositories.
    Otherwise the cache is rebuilt only when Blender's own module list changed
    (e.g. after installing an extension), which is cheap to detect.
    """
    global _entries, _by_module, _signature
    try:
        modules = list(addon_utils.modules(refresh=refresh))
    except Exception:
        traceback.print_exc()
        return _entries

    signature = tuple(mod.__name__ for mod in modules)
    if refresh or signature != _signature:
        _entries = _build_entries(modules)
        _by_module = {e.module: e for e in _entries}
        _signature = signature
    return _entries


def find_entry(module: str) -> AddonEntry | None:
    if module not in _by_module:
        get_entries()
    return _by_module.get(module)


def clear_cache() -> None:
    global _entries, _by_module, _signature, _sidebar_signature, _policy_signature
    _entries, _by_module, _signature = [], {}, ()
    _sidebar_signature = _policy_signature = None
    sidebar.reset()


# -----------------------------------------------------------------------------
# State


def is_loaded(module: str) -> bool:
    """Check Blender's runtime registered flag without noisy per-draw checks."""
    mod = sys.modules.get(module)
    return bool(mod is not None and getattr(mod, "__addon_enabled__", False))


def checked_loaded(module: str) -> bool:
    """Authoritative check after state changes (not on every UI redraw)."""
    try:
        return bool(addon_utils.check(module)[1])
    except Exception:
        return is_loaded(module)


def get_state(module: str, enabled_in_prefs=None) -> str:
    """STATE_ON, STATE_OFF or STATE_ERROR for an add-on module.

    ``enabled_in_prefs`` may be a pre-built set of module names, to avoid
    re-reading the preferences for every row when drawing a long list.
    """
    if is_loaded(module):
        return STATE_ON
    if enabled_in_prefs is None:
        enabled_in_prefs = bpy.context.preferences.addons
    return STATE_ERROR if module in enabled_in_prefs else STATE_OFF


# -----------------------------------------------------------------------------
# Enable / disable


def _short_error(ex: BaseException) -> str:
    text = "{:s}: {:s}".format(type(ex).__name__, str(ex)) if str(ex) else type(ex).__name__
    first_line = text.strip().splitlines()[0] if text.strip() else type(ex).__name__
    return first_line if len(first_line) <= 220 else first_line[:217] + "..."


def set_enabled(module: str, enable: bool) -> ToggleResult:
    """Switch add-ons through Blender's supported API, verify actual state.

    The organizer temporarily re-registers third-party panels to hide their
    original tabs. Restore them before disabling to avoid unregister conflicts;
    the caller reapplies the sidebar policy once the toggle has completed.
    """
    if module == OWN_MODULE:
        return ToggleResult(False, "The organizer cannot disable itself from its own panel.")

    errors: list[str] = []

    def on_error(ex: Exception) -> None:
        traceback.print_exception(type(ex), ex, ex.__traceback__)
        errors.append(_short_error(ex))

    try:
        prefs_addons = bpy.context.preferences.addons
        loaded = checked_loaded(module)
        saved = module in prefs_addons

        if enable:
            if loaded and saved:
                return ToggleResult(True)
            # Blender can mark an already loaded add-on as persistent without
            # unregistering it: avoid discarding live state unnecessarily.
            mod = addon_utils.enable(module, default_set=True, handle_error=on_error)
            loaded = checked_loaded(module)
            saved = module in bpy.context.preferences.addons
            if not loaded or not saved or mod is None:
                return ToggleResult(False, errors[-1] if errors else "Blender could not register and save this add-on as enabled.")
            warning = ""
            try:
                required = tuple(addon_utils.module_bl_info(mod).get("blender", ()))
                if required and required > tuple(bpy.app.version):
                    warning = "Requires Blender {:s}; it might not work correctly.".format(
                        ".".join(str(v) for v in required))
            except Exception:
                pass
            return ToggleResult(True, warning=warning)

        if not loaded and not saved:
            return ToggleResult(True)

        # Restore native panel types before the target add-on unregisters them.
        # Rescan as the map can be stale if the add-on was enabled elsewhere.
        try:
            sidebar.scan({module})
            if not sidebar.unpark(sidebar.panels_of(module)):
                return ToggleResult(False, "Could not restore its sidebar panels before disabling.")
        except Exception as ex:
            traceback.print_exc()
            return ToggleResult(False, "Could not restore sidebar panels: " + _short_error(ex))

        addon_utils.disable(module, default_set=True, handle_error=on_error)
        loaded = checked_loaded(module)
        saved = module in bpy.context.preferences.addons
        if loaded or saved:
            return ToggleResult(False, errors[-1] if errors else "Blender still reports it as enabled.")
        if errors:
            return ToggleResult(True, warning=(
                "Disabled, but unregister() reported an issue: {:s}. Restart Blender if UI remains.".format(errors[-1])))
        return ToggleResult(True)
    except Exception as ex:
        traceback.print_exc()
        return ToggleResult(False, _short_error(ex))


# -----------------------------------------------------------------------------
# Preferences of the organizer itself


def get_preferences(context=None):
    """The organizer's own add-on preferences, or None if not available."""
    try:
        return (context or bpy.context).preferences.addons[OWN_MODULE].preferences
    except (KeyError, AttributeError):
        return None


def hide_native_tabs_enabled() -> bool:
    prefs = get_preferences()
    return True if prefs is None else bool(prefs.hide_native_tabs)


def has_preferences(module: str) -> bool:
    """True if the add-on is enabled and has preferences of its own."""
    try:
        return bpy.context.preferences.addons[module].preferences is not None
    except (KeyError, AttributeError):
        return False


def tag_redraw_all() -> None:
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            area.tag_redraw()


# -----------------------------------------------------------------------------
# N-Panel (3D Viewport sidebar): tab memory and visibility policy
#
# The real work (detect / park / embed) lives in sidebar.py.  Here it is tied
# to the list of enabled add-ons and to the user's settings.

def managed_modules(entries) -> frozenset:
    """Enabled add-ons whose sidebar panels the organizer looks after.

    Blender-internal helper add-ons are left alone (they are not even listed)
    unless Developer Extras is on -- a tab hidden there could not be reached.
    """
    show_internal = bpy.context.preferences.view.show_developer_ui
    return frozenset(
        e.module for e in entries
        if is_loaded(e.module) and (show_internal or not e.is_internal)
    )


_sidebar_signature: frozenset | None = None
_policy_signature: tuple | None = None
_tick_count = 0


def detect_sidebar_tabs(candidates) -> dict[str, list[str]]:
    """Map add-on module -> sorted tab names of its registered sidebar panels."""
    return sidebar.tabs_from(sidebar.scan(candidates, update_cache=False))


def sync_sidebar_tabs(entries=None, force: bool = False) -> bool:
    """Refresh the panel cache and the remembered tabs when the enabled add-ons changed.

    Cheap to call from draw code: the (small) scan only runs when the set of
    loaded add-ons differs from the last scan.  Returns True if data changed.
    """
    global _sidebar_signature
    if entries is None:
        entries = get_entries()
    loaded = managed_modules(entries)
    if not force and loaded == _sidebar_signature:
        return False
    _sidebar_signature = loaded
    changed = storage.update_sidebar_tabs(sidebar.tabs_from(sidebar.scan(loaded)), loaded)
    if changed:
        storage.schedule_save()
    return changed


def enforce_sidebar_policy(force: bool = False) -> bool:
    """Hide (or show) the managed add-ons' own sidebar tabs according to the settings.

    Only does work when the enabled add-ons or the settings changed (or when
    ``force``).  Must not be called from draw code: it re-registers panels.
    """
    global _policy_signature
    entries = get_entries()
    loaded = managed_modules(entries)
    hide = hide_native_tabs_enabled()
    keep = frozenset(storage.keep_tabs())
    signature = (loaded, hide, keep)
    if not force and signature == _policy_signature:
        return False
    _policy_signature = signature

    sync_sidebar_tabs(entries, force=True)
    sidebar.apply_policy(hide, keep)
    tag_redraw_all()
    return True


def _enforce_once():
    try:
        enforce_sidebar_policy(force=True)
    except Exception:
        traceback.print_exc()
    return None  # run once


def request_enforce() -> None:
    """Apply the sidebar policy from a timer (safe to call from property callbacks)."""
    if not bpy.app.timers.is_registered(_enforce_once):
        bpy.app.timers.register(_enforce_once, first_interval=0.0, persistent=True)


def _tick():
    """Twice a second: notice add-ons enabled/disabled elsewhere (e.g. Preferences)
    and hide the sidebar tabs of newly enabled ones.  A rescan is forced every ~10 s
    to catch add-ons that register panels late."""
    global _tick_count
    _tick_count += 1
    try:
        enforce_sidebar_policy(force=(_tick_count % 20 == 0))
    except Exception:
        traceback.print_exc()
    return 0.5


def start() -> None:
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.3, persistent=True)


def stop() -> None:
    """Switch everything off and give every add-on its own sidebar tab back."""
    for timer in (_tick, _enforce_once):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    storage.flush_scheduled_save()
    try:
        sidebar.restore_all()
    except Exception:
        traceback.print_exc()
    clear_cache()