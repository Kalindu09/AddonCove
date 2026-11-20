# SPDX-License-Identifier: GPL-3.0-or-later
"""Other add-ons' 3D Viewport sidebar (N-Panel) panels.

Three jobs, all done at *runtime* and never touching an add-on's files:

1. **Detect** which registered sidebar panels belong to which add-on (Blender
   has no API for this; the Python module of the panel class is used).
2. **Park** them: the panel classes are re-registered in the 3D Viewport's
   *header* region (which only draws Header classes, never panels), so their
   tab disappears from the N-Panel.  This needs ``bl_category`` to be cleared
   while parked.  Everything is remembered and put back by ``unpark()`` /
   ``restore_all()`` -- also when the organizer is disabled.
3. **Embed**: draw a parked add-on's panels inside the organizer by calling
   the panel's own ``draw()`` / ``draw_header()`` / ``poll()`` with a stand-in
   for ``self``.

Class registration is changed only from timers and operators, never from
draw code (Blender frees panel types when they are unregistered).
"""

import traceback
import types

import bpy


OP = "addoncove"
_PARK_REGION = 'HEADER'


# -----------------------------------------------------------------------------
# Panel class helpers


def _idname(cls) -> str:
    return getattr(cls, "bl_idname", "") or cls.__name__


def _parent(cls) -> str:
    value = getattr(cls, "bl_parent_id", "")
    return value if isinstance(value, str) else ""


def _order(cls) -> int:
    value = getattr(cls, "bl_order", 0)
    return value if isinstance(value, int) else 0


def _label(cls) -> str:
    value = getattr(cls, "bl_label", "")
    return value if isinstance(value, str) and value else cls.__name__


def _options(cls) -> set:
    value = getattr(cls, "bl_options", ())
    return set(value) if isinstance(value, (set, frozenset, list, tuple)) else set()


def _str_attr(cls, name: str) -> str:
    value = getattr(cls, name, "")
    return value if isinstance(value, str) else ""


def iter_panel_classes():
    """Every Python Panel subclass, including indirect ones (shared base classes)."""
    seen = set()
    stack = [bpy.types.Panel]
    while stack:
        base = stack.pop()
        try:
            subclasses = base.__subclasses__()
        except TypeError:
            continue
        for cls in subclasses:
            if cls not in seen:
                seen.add(cls)
                stack.append(cls)
                yield cls


def _owner_module(dotted_name: str, candidates) -> str:
    name = dotted_name
    while name:
        if name in candidates:
            return name
        name = name.rpartition(".")[0]
    return ""


def _ordered(classes) -> list:
    """Parents before children (registration order kept within one depth)."""
    by_id = {_idname(c): c for c in classes}

    def depth(cls):
        level, seen, parent = 0, set(), _parent(cls)
        while parent in by_id and parent not in seen:
            seen.add(parent)
            level += 1
            parent = _parent(by_id[parent])
        return level

    return sorted(classes, key=depth)


# -----------------------------------------------------------------------------
# Parking state


class _Raw:
    """The class attributes that parking changes (to restore them exactly)."""
    __slots__ = ("region", "category_in_dict", "category_value", "category")

    def __init__(self, cls):
        self.region = cls.bl_region_type
        self.category_in_dict = "bl_category" in cls.__dict__
        self.category_value = cls.__dict__.get("bl_category")
        self.category = _str_attr(cls, "bl_category")  # effective value (may be inherited)

    def apply(self, cls) -> None:
        cls.bl_region_type = self.region
        if self.category_in_dict:
            cls.bl_category = self.category_value
        elif "bl_category" in cls.__dict__:
            delattr(cls, "bl_category")


_parked: dict = {}                     # panel class -> _Raw (its original state)
_by_module: dict[str, list] = {}       # add-on module -> its sidebar panel classes
_failed: set[str] = set()              # modules we could not park (not retried every tick)
_panel_open: dict[str, bool] = {}      # "module|idname" -> open state of an embedded panel
_printed: set[str] = set()             # draw errors already printed to the console


def _is_sidebar_panel(cls) -> bool:
    if not getattr(cls, "is_registered", False):
        return False
    if cls in _parked:
        return True
    return getattr(cls, "bl_space_type", "") == 'VIEW_3D' and getattr(cls, "bl_region_type", "") == 'UI'


def original_category(cls) -> str:
    raw = _parked.get(cls)
    return raw.category if raw is not None else _str_attr(cls, "bl_category")


# -----------------------------------------------------------------------------
# Detection


def scan(candidates, update_cache: bool = True) -> dict[str, list]:
    """Map add-on module -> its registered sidebar panel classes (parked ones included)."""
    found: dict[str, list] = {}
    for cls in iter_panel_classes():
        if _is_sidebar_panel(cls):
            owner = _owner_module(getattr(cls, "__module__", ""), candidates)
            if owner:
                found.setdefault(owner, []).append(cls)
    if update_cache:
        _by_module.clear()
        _by_module.update(found)
        _failed.intersection_update(found)
    return found


def tabs_from(found: dict[str, list]) -> dict[str, list[str]]:
    """Map add-on module -> sorted names of the tabs its top-level panels use."""
    tabs = {}
    for module, classes in found.items():
        names = {original_category(c) for c in classes if not _parent(c)}
        names.discard("")
        if names:
            tabs[module] = sorted(names, key=str.casefold)
    return tabs


def panels_of(module: str) -> list:
    return [c for c in _by_module.get(module, ()) if c.is_registered]


def has_panels(module: str) -> bool:
    return any(c.is_registered for c in _by_module.get(module, ()))


# -----------------------------------------------------------------------------
# Parking / restoring


def _swap(group: list, mutate) -> None:
    """Unregister ``group`` (children first), mutate each class, register again.

    On any failure the previous state is put back (best effort) and the
    exception is re-raised, so a failed swap never leaves an add-on without
    its panels.
    """
    before = {c: _Raw(c) for c in group}
    try:
        for cls in reversed(group):
            bpy.utils.unregister_class(cls)
        for cls in group:
            mutate(cls)
        for cls in group:
            bpy.utils.register_class(cls)
    except Exception:
        for cls in reversed(group):
            if cls.is_registered:
                try:
                    bpy.utils.unregister_class(cls)
                except Exception:
                    pass
        for cls in group:
            try:
                before[cls].apply(cls)
            except Exception:
                pass
        for cls in group:
            if not cls.is_registered:
                try:
                    bpy.utils.register_class(cls)
                except Exception:
                    traceback.print_exc()
        raise


def _park_attrs(cls) -> None:
    cls.bl_region_type = _PARK_REGION
    cls.bl_category = ""  # panels in a header region must not have a category


def park(classes) -> bool:
    """Hide the sidebar tab of these panels.  Returns False if Blender refused."""
    group = [c for c in _ordered(classes) if c.is_registered and c not in _parked]
    if not group:
        return True
    saved = {c: _Raw(c) for c in group}
    try:
        _swap(group, _park_attrs)
    except Exception:
        print("AddonCove: could not hide the sidebar tab of", group[0].__module__)
        traceback.print_exc()
        return False
    _parked.update(saved)
    return True


def unpark(classes) -> bool:
    """Give these panels their own sidebar tab back."""
    parked = [c for c in _ordered(classes) if c in _parked]
    if not parked:
        return True
    ok = True
    for cls in [c for c in parked if not c.is_registered]:
        try:  # add-on is off: only the attributes need restoring
            _parked.pop(cls).apply(cls)
        except Exception:
            traceback.print_exc()
            ok = False
    live = [c for c in parked if c.is_registered]
    if live:
        try:
            _swap(live, lambda cls: _parked[cls].apply(cls))
        except Exception:
            print("AddonCove: could not restore the sidebar tab of", live[0].__module__)
            traceback.print_exc()
            return False
        for cls in live:
            _parked.pop(cls, None)
    return ok


def restore_all() -> bool:
    """Undo all parking (used when the organizer is switched off or hiding is disabled)."""
    ok = unpark(list(_parked))
    _failed.clear()
    return ok


def apply_policy(hide: bool, keep) -> None:
    """Park every managed add-on's sidebar panels, except the add-ons in ``keep``."""
    if not hide:
        restore_all()
        return
    for module, classes in list(_by_module.items()):
        if module in keep:
            unpark(classes)
        elif module not in _failed and not park(classes):
            _failed.add(module)

    # Parked panels of an add-on that is no longer managed (e.g. Developer
    # Extras was switched off) would have no way back: restore them.
    managed = {c for classes in _by_module.values() for c in classes}
    unpark([c for c in _parked if c.is_registered and c not in managed])


def reset() -> None:
    _by_module.clear()
    _failed.clear()
    _panel_open.clear()
    _printed.clear()


# -----------------------------------------------------------------------------
# Embedding: draw another add-on's panels inside the organizer


class _Stand:
    """Stands in for the Panel instance (``self``) while its draw code runs here.

    ``self.layout`` is the organizer's layout; everything else (class
    attributes, helper methods, mix-ins) is looked up on the real panel class.
    """
    is_popover = False

    def __init__(self, cls, layout):
        self._cls = cls
        self.layout = layout

    def __getattr__(self, name):
        value = getattr(self._cls, name)
        return types.MethodType(value, self) if isinstance(value, types.FunctionType) else value


def _report_once(cls, ex: Exception) -> None:
    signature = "{}.{}: {}: {}".format(cls.__module__, cls.__name__, type(ex).__name__, ex)
    if signature not in _printed:
        _printed.add(signature)
        print("AddonCove: panel '{}' failed to draw inside the organizer:".format(_label(cls)))
        traceback.print_exc()


def _call(cls, func, layout, context) -> None:
    try:
        func(_Stand(cls, layout), context)
    except Exception as ex:
        _report_once(cls, ex)
        layout.label(text="This panel could not be drawn (see console)", icon='ERROR')


def _poll_ok(cls, context) -> bool:
    poll = getattr(cls, "poll", None)
    if poll is None:
        return True
    try:
        return bool(poll(context))
    except Exception as ex:
        _report_once(cls, ex)
        return False


def toggle_panel(key: str, default_open: bool) -> None:
    _panel_open[key] = not _panel_open.get(key, default_open)


def _draw_panel(layout, context, module: str, cls, classes: list, bare: bool, nested: bool) -> bool:
    if not _poll_ok(cls, context):
        return False

    idname = _idname(cls)
    options = _options(cls)
    draw = getattr(cls, "draw", None)
    draw_header = getattr(cls, "draw_header", None)
    children = sorted((c for c in classes if _parent(c) == idname), key=_order)

    no_header = 'HIDE_HEADER' in options or (bare and not callable(draw_header))
    default_open = 'DEFAULT_CLOSED' not in options
    key = module + "|" + idname
    is_open = True if no_header else _panel_open.get(key, default_open)

    if no_header:
        body = layout.column()
    else:
        container = layout if nested else layout.box()
        row = container.row()
        left = row.row()
        left.alignment = 'LEFT'
        op = left.operator(
            OP + ".panel_toggle",
            text=_label(cls),
            icon='DISCLOSURE_TRI_DOWN' if is_open else 'DISCLOSURE_TRI_RIGHT',
            emboss=False,
            translate=False,
        )
        op.key, op.default_open = key, default_open
        if callable(draw_header):
            right = row.row()
            right.alignment = 'RIGHT'
            _call(cls, draw_header, right, context)
        body = container.column() if is_open else None

    if body is not None:
        if callable(draw):
            _call(cls, draw, body, context)
        for child in children:
            _draw_panel(body, context, module, child, classes, bare=False, nested=True)
    return True


def draw_panels(layout, context, module: str) -> int:
    """Draw all sidebar panels of an add-on.  Returns how many were drawn."""
    classes = panels_of(module)
    ids = {_idname(c) for c in classes}
    top_level = sorted((c for c in classes if _parent(c) not in ids), key=_order)
    bare = len(top_level) == 1  # a single panel needs no extra header: the add-on row is the header
    return sum(1 for cls in top_level if _draw_panel(layout, context, module, cls, classes, bare, nested=False))