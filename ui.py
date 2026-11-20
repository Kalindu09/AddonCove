# SPDX-License-Identifier: GPL-3.0-or-later
"""The 3D Viewport sidebar (N-Panel) user interface."""

import textwrap
import traceback

import bpy
from bpy.props import BoolProperty, EnumProperty, PointerProperty, StringProperty
from bpy.types import Panel, PropertyGroup
from bpy.app.handlers import persistent

from . import addons, operators, sidebar, storage

OP = operators.IDNAME_PREFIX

# Approximate metrics used to decide where to wrap long names (UI scale 1).
_CHAR_WIDTH_PX = 6.6
_RESERVED_WIDTH_PX = 116


# -----------------------------------------------------------------------------
# UI state (session only -- filters are not persisted)


class ADDONCOVE_UIState(PropertyGroup):
    state_filter: EnumProperty(
        name="Show",
        description="Show only add-ons that are on or off",
        items=[
            ('ALL', "All", "Show all add-ons"),
            ('ON', "On", "Show only enabled add-ons"),
            ('OFF', "Off", "Show only disabled add-ons"),
        ],
        default='ALL',
    )
    only_sidebar: BoolProperty(
        name="N-Panel Only",
        description=(
            "Show only add-ons that add a tab to the 3D Viewport sidebar (N-Panel). "
            "Detected from their registered panels, so an add-on appears here once it has been enabled"
        ),
        # A fresh Blender session or newly loaded .blend should start with
        # N-Panel Only enabled. Do not keep an old OFF value in saved .blend data.
        default=True,
        options={'SKIP_SAVE'},
    )
    search_text: StringProperty(
        name="Search Add-ons",
        description="Search add-on names, module IDs, authors, and descriptions",
        default="",
        options={'TEXTEDIT_UPDATE', 'SKIP_SAVE'},
    )


# -----------------------------------------------------------------------------
# Drawing helpers


def wrap_lines(text: str, max_chars: int) -> list[str]:
    """Split a name into lines of at most ``max_chars`` (0 = never wrap)."""
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    lines = textwrap.wrap(text, width=max_chars, break_long_words=True, break_on_hyphens=True)
    return lines or [text]


def wrap_width_for(context, prefs) -> int:
    """How many characters fit on a row of the current sidebar width."""
    if prefs is not None and not prefs.wrap_names:
        return 0
    region = context.region
    if region is None or region.width <= 0:
        return 0
    scale = context.preferences.system.ui_scale or 1.0
    usable = region.width / scale - _RESERVED_WIDTH_PX
    return max(14, int(usable / _CHAR_WIDTH_PX))


class _Row:
    """One add-on as it is shown (pre-computed once per redraw)."""
    __slots__ = ("entry", "name", "state", "category")

    def __init__(self, entry, name, state, category):
        self.entry = entry
        self.name = name
        self.state = state
        self.category = category


def draw_toolbar(layout, context, state, entries) -> None:
    """Search, visibility filters, and compact icon-only actions."""
    # Use the theme's regular N-panel background. UI boxes paint a second
    # (usually darker) surface, which looks out of place beside native panels.
    toolbar = layout.column(align=True)

    # Search is kept in its own row so the field remains usable in a narrow N-panel.
    search_row = toolbar.row(align=True)
    search_row.prop(state, "search_text", text="", icon='VIEWZOOM')
    search_row.operator(OP + ".refresh", text="", icon='FILE_REFRESH')

    install_row = toolbar.row(align=True)
    install_row.operator_context = 'INVOKE_DEFAULT'
    install_row.operator(OP + ".install", text="Install Add-on (.zip / .py)", icon='IMPORT')

    filter_row = toolbar.row(align=True)
    filter_row.prop(state, "state_filter", expand=True)

    action_row = toolbar.row(align=True)
    action_row.prop(state, "only_sidebar", text="N-Panel only", icon='MENU_PANEL')

    bulk_row = toolbar.row(align=True)
    op = bulk_row.operator(OP + ".all_toggle", text="Enable All", icon='CHECKBOX_HLT')
    op.enable = True
    op = bulk_row.operator(OP + ".all_toggle", text="Disable All", icon='CHECKBOX_DEHLT')
    op.enable = False

    # Category creation remains available without the old menu/drop-down button.
    action_row.operator(OP + ".category_new", text="", icon='ADD')


def draw_item(layout, context, row_data: _Row, wrap_chars: int) -> None:
    """Draw one add-on with a dedicated checkbox, name, and options menu."""
    entry, state = row_data.entry, row_data.state
    lines = wrap_lines(row_data.name, wrap_chars)
    can_expand = state == addons.STATE_ON and sidebar.has_panels(entry.module)
    expanded = can_expand and storage.is_expanded(entry.module)

    block = layout.column(align=True)
    block.alert = state == addons.STATE_ERROR

    if state == addons.STATE_OFF:
        state_icon = 'CHECKBOX_DEHLT'
    elif state == addons.STATE_ERROR:
        state_icon = 'ERROR'
    else:
        state_icon = 'CHECKBOX_HLT'

    for index, line in enumerate(lines):
        row = block.row(align=True)

        if index == 0 and can_expand:
            row.operator(
                OP + ".expand_toggle", text="", emboss=False,
                icon='DISCLOSURE_TRI_DOWN' if expanded else 'DISCLOSURE_TRI_RIGHT',
            ).module = entry.module
        else:
            row.label(text="", icon='BLANK1')

        # The checkbox is its own real button; clicking the add-on name no longer
        # relies on an invisible/tiny toggle area.
        toggle = row.operator(
            OP + ".toggle", text="", icon=state_icon, emboss=False,
        )
        toggle.module = entry.module
        # STATE_ERROR means it is listed as enabled in preferences but failed
        # to load. Clicking its icon should cleanly disable that stale entry.
        toggle.enable = state == addons.STATE_OFF

        row.label(text=line, translate=False)

        if index == 0:
            row.separator(factor=0.35)
            row.operator(
                OP + ".item_menu", text="", icon='DOWNARROW_HLT', emboss=False,
            ).module = entry.module

    if expanded:
        # Embed third-party controls directly on the normal N-panel surface.
        embedded = layout.column(align=True)
        if sidebar.draw_panels(embedded, context, entry.module) == 0:
            col = embedded.column()
            col.active = False
            col.label(text="Nothing to show in the current mode.", icon='INFO')


def draw_section_header(box, key: str, label: str, enabled: int, total: int, is_open: bool) -> None:
    """Draw a category title, a compact status count and its options menu."""
    row = box.row(align=True)
    left = row.row(align=True)
    left.alignment = 'LEFT'
    left.operator(
        OP + ".category_toggle",
        text=label,
        icon='DISCLOSURE_TRI_DOWN' if is_open else 'DISCLOSURE_TRI_RIGHT',
        emboss=False,
        translate=False,
    ).category = key

    right = row.row(align=True)
    right.alignment = 'RIGHT'
    right.label(text=f"{enabled}/{total}", icon='CHECKMARK')
    if key != storage.UNCATEGORIZED:
        right.operator(
            OP + ".category_menu", text="", icon='DOWNARROW_HLT', emboss=False,
        ).category = key


# -----------------------------------------------------------------------------
# Main panel


class ADDONCOVE_PT_main(Panel):
    bl_idname = "ADDONCOVE_PT_main"
    bl_label = "AddonCove"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AddonCove"

    _last_error_printed = ""

    def draw(self, context):
        try:
            self._draw(context)
        except Exception as ex:  # never leave an empty tab without explanation
            self.layout.label(text="AddonCove failed to draw", icon='ERROR')
            self.layout.label(text="See the system console for details.")
            message = "{:s}: {:s}".format(type(ex).__name__, str(ex))
            if message != ADDONCOVE_PT_main._last_error_printed:
                ADDONCOVE_PT_main._last_error_printed = message
                traceback.print_exc()

    def _draw(self, context):
        layout = self.layout
        state = context.window_manager.addoncove
        prefs = addons.get_preferences(context)

        entries = addons.get_entries()
        addons.sync_sidebar_tabs(entries)

        show_internal = context.preferences.view.show_developer_ui
        enabled_in_prefs = set(context.preferences.addons.keys())

        # Build every visible row once.
        all_rows = []
        for entry in entries:
            if entry.is_internal and not show_internal:
                continue
            all_rows.append(_Row(
                entry,
                storage.display_name(entry.module, entry.name),
                addons.get_state(entry.module, enabled_in_prefs),
                storage.category_of(entry.module),
            ))
        all_rows.sort(key=lambda r: (r.name.casefold(), r.entry.module))

        state_filter = state.state_filter
        search_query = state.search_text.strip().casefold()
        filtering = state_filter != 'ALL' or state.only_sidebar or bool(search_query)
        draw_toolbar(layout, context, state, entries)

        # Per-category totals ignore filters, so the on/total count stays stable.
        totals: dict[str, list[int]] = {}
        shown: dict[str, list[_Row]] = {}
        for row in all_rows:
            counts = totals.setdefault(row.category, [0, 0])
            counts[1] += 1
            if row.state != addons.STATE_OFF:
                counts[0] += 1

            if state_filter == 'ON' and row.state == addons.STATE_OFF:
                continue
            if state_filter == 'OFF' and row.state != addons.STATE_OFF:
                continue
            if state.only_sidebar and not storage.sidebar_tabs(row.entry.module):
                continue
            if search_query:
                searchable = " ".join((
                    row.name,
                    row.entry.name,
                    row.entry.module,
                    row.entry.author,
                    row.entry.description,
                    row.entry.version,
                )).casefold()
                if search_query not in searchable:
                    continue
            shown.setdefault(row.category, []).append(row)

        layout.separator()
        if not all_rows:
            empty = layout.column(align=True)
            empty.label(text="No add-ons found.", icon='INFO')
            empty.operator(OP + ".refresh", text="Refresh List", icon='FILE_REFRESH')
            return
        if not shown:
            empty = layout.column(align=True)
            empty.label(text="No add-ons match these filters.", icon='INFO')
            empty.operator(OP + ".clear_filters", text="Clear Filters", icon='X')
            return

        wrap_chars = wrap_width_for(context, prefs)
        categories = storage.categories()

        if not categories:
            box = layout.column(align=True)
            row = box.row(align=True)
            row.label(text="All Add-ons", icon='PREFERENCES')
            count = row.row(align=True)
            count.alignment = 'RIGHT'
            count.label(text=str(len(shown.get(storage.UNCATEGORIZED, ()))))
            body = box.column(align=True)
            rows = shown.get(storage.UNCATEGORIZED, ())
            if rows:
                for item in rows:
                    draw_item(body, context, item, wrap_chars)
            else:
                body.label(text="No add-ons in this view.", icon='INFO')
            if not filtering:
                hint = layout.row()
                hint.scale_y = 0.85
                hint.label(text="Use Category + to create your first group.", icon='INFO')
            return

        for key in (*categories, storage.UNCATEGORIZED):
            rows = shown.get(key, [])
            if not rows and (filtering or key == storage.UNCATEGORIZED):
                continue  # hide empty groups while filtering and empty Uncategorized

            enabled, total = totals.get(key, (0, 0))
            is_open = not storage.is_collapsed(key)
            label = storage.UNCATEGORIZED_LABEL if key == storage.UNCATEGORIZED else key

            box = layout.column(align=True)
            draw_section_header(box, key, label, enabled, total, is_open)
            if is_open:
                body = box.column(align=True)
                if rows:
                    for item in rows:
                        draw_item(body, context, item, wrap_chars)
                else:
                    body.label(text="No add-ons assigned", icon='INFO')
            layout.separator()


# -----------------------------------------------------------------------------

classes = (ADDONCOVE_UIState, ADDONCOVE_PT_main)


def _reset_new_session_filter():
    """Set N-Panel Only to ON *after* Blender finishes add-on registration.

    When an extension is enabled from Preferences, Blender deliberately wraps
    bpy.data / bpy.context in restricted proxy objects during register().
    Looking up window managers at that time raises _RestrictData errors.
    Timer callbacks run later, when normal context access is available.
    """
    try:
        wm = bpy.context.window_manager
        if wm is not None:
            ui_state = getattr(wm, "addoncove", None)
            if ui_state is not None:
                ui_state.only_sidebar = True
    except (AttributeError, ReferenceError, RuntimeError):
        # For background/headless operation, the BoolProperty(default=True)
        # already provides the correct value when a UI is created later.
        pass
    return None  # a one-shot timer


def _schedule_new_session_filter():
    """Schedule the reset without touching any restricted Blender data."""
    timers = bpy.app.timers
    if not timers.is_registered(_reset_new_session_filter):
        timers.register(_reset_new_session_filter, first_interval=0.1)


@persistent
def _reset_filter_after_load(_dummy):
    # The load handler may run before a window context is restored; defer too.
    _schedule_new_session_filter()


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.addoncove = PointerProperty(type=ADDONCOVE_UIState)
    handlers = bpy.app.handlers.load_post
    if _reset_filter_after_load not in handlers:
        handlers.append(_reset_filter_after_load)
    # Never query bpy.context or bpy.data inside register(); these can be
    # _RestrictContext / _RestrictData during an extension installation.
    _schedule_new_session_filter()


def unregister() -> None:
    handlers = bpy.app.handlers.load_post
    if _reset_filter_after_load in handlers:
        handlers.remove(_reset_filter_after_load)
    if bpy.app.timers.is_registered(_reset_new_session_filter):
        bpy.app.timers.unregister(_reset_new_session_filter)
    if hasattr(bpy.types.WindowManager, "addoncove"):
        del bpy.types.WindowManager.addoncove
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass  # was never registered (partial register)