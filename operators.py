# SPDX-License-Identifier: GPL-3.0-or-later
"""Operators and popup menus of the organizer.

All operators are ``INTERNAL`` (they only exist to be clicked in the panel, so
they don't clutter the F3 search).  Operators that need an add-on or category
take it as a hidden property.
"""

import traceback
from pathlib import Path

from bpy_extras.io_utils import ImportHelper

import addon_utils
import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator

from . import addons, sidebar, storage

IDNAME_PREFIX = "addoncove"


# -----------------------------------------------------------------------------
# Helpers


def _tag_redraw(context=None) -> None:
    wm = (context or bpy.context).window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def _wait_cursor(show: bool) -> None:
    # Use the active WindowManager instead of bpy.data.window_managers so the
    # helper also works when Blender restricts the global data API.
    try:
        wm = bpy.context.window_manager
    except (AttributeError, ReferenceError, RuntimeError):
        return
    if wm is None:
        return
    for window in wm.windows:
        try:
            if show:
                window.cursor_modal_set('WAIT')
            else:
                window.cursor_modal_restore()
        except Exception:
            pass


def shown_name(module: str) -> str:
    entry = addons.find_entry(module)
    return storage.display_name(module, entry.name if entry else module)


def _report_save_problem(op) -> None:
    error = storage.last_error()
    if error:
        op.report({'WARNING'}, error)


def addon_tooltip(module: str, enable: bool | None = None) -> str:
    """Multi-line tooltip with everything useful about an add-on."""
    entry = addons.find_entry(module)
    if entry is None:
        return "Add-on not found. Try Refresh."
    name = storage.display_name(module, entry.name)
    state = addons.get_state(module)

    lines = []
    if enable is not None:
        lines.append("{:s} \"{:s}\"".format("Enable" if enable else "Disable", name))
    else:
        lines.append(name)
    if state == addons.STATE_ERROR:
        lines.append("Enabled in your preferences but it failed to load (see the system console).")
        lines.append("Click to switch it off.")
    if entry.description:
        lines.append(entry.description)
    if storage.custom_name(module):
        lines.append("Original name: {:s}".format(entry.name))
    details = []
    if entry.version:
        details.append("v" + entry.version)
    if entry.author:
        details.append(entry.author)
    details.append("Extension" if entry.is_extension else "Legacy add-on")
    lines.append(" \u00b7 ".join(details))
    lines.append("Module: " + module)
    tabs = storage.sidebar_tabs(module)
    if tabs:
        lines.append("N-Panel tab: " + ", ".join(tabs))
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Enable / disable


class ADDONCOVE_OT_toggle(Operator):
    bl_idname = IDNAME_PREFIX + ".toggle"
    bl_label = "Enable / Disable Add-on"
    bl_description = "Enable or disable this add-on"
    bl_options = {'INTERNAL'}

    module: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    enable: BoolProperty(
        name="Enable",
        description="The state to switch the add-on to",
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    @classmethod
    def description(cls, context, properties):
        try:
            return addon_tooltip(properties.module, properties.enable)
        except Exception:
            return "Enable or disable this add-on"

    def execute(self, context):
        module = self.module
        entry = addons.find_entry(module)
        if entry is None:
            self.report({'ERROR'}, "Add-on \"{:s}\" was not found. Try Refresh from the menu.".format(module))
            return {'CANCELLED'}

        name = storage.display_name(module, entry.name)
        verb = "enable" if self.enable else "disable"

        policy_error = ""
        _wait_cursor(True)
        try:
            result = addons.set_enabled(module, self.enable)
            if result.ok:
                try:
                    addons.enforce_sidebar_policy(force=True)
                except Exception as ex:
                    traceback.print_exc()
                    policy_error = str(ex)
        finally:
            _wait_cursor(False)
        _tag_redraw(context)

        if not result.ok:
            try:
                addons.enforce_sidebar_policy(force=True)
            except Exception:
                traceback.print_exc()
            self.report({'ERROR'}, "Could not {:s} \"{:s}\": {:s} (details are in the system console)".format(
                verb, name, result.message))
            return {'CANCELLED'}
        if result.warning:
            self.report({'WARNING'}, "\"{:s}\": {:s}".format(name, result.warning))
        if policy_error:
            self.report({'WARNING'}, "Add-on state changed, but the sidebar refresh failed: {:s}".format(policy_error))
        return {'FINISHED'}



class ADDONCOVE_OT_all_toggle(Operator):
    """Enable or disable all visible add-ons without undocumented internals."""
    bl_idname = IDNAME_PREFIX + ".all_toggle"
    bl_label = "Enable / Disable All Add-ons"
    bl_options = {'INTERNAL'}

    enable: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        entries = list(addons.get_entries(refresh=True))
        succeeded, failed = 0, []
        _wait_cursor(True)
        try:
            # Disable in reverse discovery order to minimize dependency clashes.
            ordered = entries if self.enable else list(reversed(entries))
            for entry in ordered:
                if entry.is_internal and not context.preferences.view.show_developer_ui:
                    continue
                result = addons.set_enabled(entry.module, self.enable)
                if result.ok:
                    succeeded += 1
                else:
                    failed.append((entry.module, result.message))
            try:
                addons.enforce_sidebar_policy(force=True)
            except Exception:
                traceback.print_exc()
        finally:
            _wait_cursor(False)

        addons.clear_cache()
        addons.get_entries(refresh=True)
        _tag_redraw(context)
        action = "enabled" if self.enable else "disabled"
        if failed:
            self.report({'WARNING'}, "{:d} add-ons {:s}, {:d} failed. See console.".format(
                succeeded, action, len(failed)))
            for module, message in failed:
                print("AddonCove: could not {} {}: {}".format(action, module, message))
        else:
            self.report({'INFO'}, "{:d} add-ons {:s}.".format(succeeded, action))
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Install and uninstall (delegate actual file management to Blender)


def _refresh_installed_list():
    """One-shot timer to catch installation/uninstallation after modal operations."""
    try:
        addons.clear_cache()
        addons.get_entries(refresh=True)
        addons.request_enforce()
        _tag_redraw()
    except Exception:
        traceback.print_exc()
    return None


def _schedule_refresh():
    if not bpy.app.timers.is_registered(_refresh_installed_list):
        bpy.app.timers.register(_refresh_installed_list, first_interval=0.7)


class ADDONCOVE_OT_install(Operator, ImportHelper):
    bl_idname = IDNAME_PREFIX + ".install"
    bl_label = "Install Add-on from Disk"
    bl_description = "Install an extension or legacy add-on from a .zip or .py file"
    bl_options = {'INTERNAL'}

    filename_ext = ".zip"
    filter_glob: StringProperty(default="*.zip;*.py", options={'HIDDEN'})
    enable_on_install: BoolProperty(name="Enable after Install", default=True)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        path = Path(bpy.path.abspath(self.filepath))
        if not path.is_file() or path.suffix.lower() not in {'.zip', '.py'}:
            self.report({'ERROR'}, "Select an existing .zip or .py add-on file.")
            return {'CANCELLED'}
        try:
            # Blender 5.2 detects both manifest-based extensions and old-style
            # add-ons. Do not unpack arbitrary ZIP files ourselves.
            result = bpy.ops.extensions.package_install_files(
                'EXEC_DEFAULT', filepath=str(path), enable_on_install=self.enable_on_install)
        except Exception as ex:
            traceback.print_exc()
            self.report({'ERROR'}, "Installation failed: " + str(ex))
            return {'CANCELLED'}

        if 'CANCELLED' in result:
            self.report({'ERROR'}, "Blender cancelled installation; see its messages.")
            return {'CANCELLED'}
        _schedule_refresh()
        if 'FINISHED' in result:
            self.report({'INFO'}, "Installation completed. Refresh the list if needed.")
        else:
            self.report({'INFO'}, "Installation started; the add-on list will refresh.")
        return {'FINISHED'}


def _user_legacy_path(module: str) -> Path | None:
    """Only allow automatic deletion of user-installed legacy modules."""
    try:
        root = bpy.utils.user_resource('SCRIPTS', path='addons', create=False)
        if not root:
            return None
        for mod in addon_utils.modules():
            if mod.__name__ != module:
                continue
            path = Path(mod.__file__).resolve()
            user_addons = Path(root).resolve()
            # Root itself is not a removable module; restrict to its contents.
            if path.is_relative_to(user_addons):
                return path
    except Exception:
        traceback.print_exc()
    return None


def _extension_repo_index(module: str) -> int | None:
    parts = module.split('.', 2)
    if len(parts) != 3 or parts[0] != 'bl_ext':
        return None
    for i, repo in enumerate(bpy.context.preferences.extensions.repos):
        if repo.module == parts[1]:
            return i
    return None


class ADDONCOVE_OT_uninstall(Operator):
    bl_idname = IDNAME_PREFIX + ".uninstall"
    bl_label = "Uninstall Add-on"
    bl_description = "Permanently remove this installed add-on from Blender (confirmation required)"
    bl_options = {'INTERNAL'}

    module: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return True

    def invoke(self, context, event):
        entry = addons.find_entry(self.module)
        if entry is None or self.module == addons.OWN_MODULE:
            self.report({'ERROR'}, "This add-on is not available for uninstall.")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(
            self, event, title="Uninstall Add-on?",
            message='Permanently uninstall "{}" from this Blender installation?'.format(
                storage.display_name(self.module, entry.name)),
            confirm_text="Uninstall", icon='ERROR')

    def execute(self, context):
        module = self.module
        entry = addons.find_entry(module)
        if entry is None or module == addons.OWN_MODULE:
            self.report({'ERROR'}, "Add-on not found or protected.")
            return {'CANCELLED'}
        if entry.is_internal:
            self.report({'ERROR'}, "Blender's internal add-ons cannot be uninstalled here.")
            return {'CANCELLED'}
        if entry.is_extension:
            repo_index = _extension_repo_index(module)
            if repo_index is None:
                self.report({'ERROR'}, "Cannot find this extension's repository.")
                return {'CANCELLED'}
        elif _user_legacy_path(module) is None:
            self.report({'ERROR'}, "Only user-installed legacy add-ons can be removed here; system files are protected.")
            return {'CANCELLED'}

        _wait_cursor(True)
        try:
            # Restore any altered panel registrations before Blender removes the
            # add-on. Uninstall is carried out by Blender, not by raw file deletion.
            sidebar.scan({module})
            if not sidebar.unpark(sidebar.panels_of(module)):
                raise RuntimeError("Could not restore the add-on's panels before uninstalling")

            if entry.is_extension:
                pkg_id = module.split('.', 2)[2]
                result = bpy.ops.extensions.package_uninstall(
                    'EXEC_DEFAULT', repo_index=repo_index, pkg_id=pkg_id)
            else:
                result = bpy.ops.preferences.addon_remove('EXEC_DEFAULT', module=module)
        except Exception as ex:
            traceback.print_exc()
            self.report({'ERROR'}, "Uninstall failed: " + str(ex))
            try:
                addons.enforce_sidebar_policy(force=True)
            except Exception:
                traceback.print_exc()
            return {'CANCELLED'}
        finally:
            _wait_cursor(False)

        if 'CANCELLED' in result:
            self.report({'ERROR'}, "Blender could not uninstall this add-on.")
            addons.request_enforce()
            return {'CANCELLED'}
        _schedule_refresh()
        if 'FINISHED' in result:
            # Clean organizer metadata once Blender reports completed removal.
            storage.remove_addon(module)
            self.report({'INFO'}, "Add-on uninstalled.")
        else:
            self.report({'INFO'}, "Uninstall started. The list will refresh.")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Renaming (display name only)


class ADDONCOVE_OT_rename(Operator):
    bl_idname = IDNAME_PREFIX + ".rename"
    bl_label = "Rename Add-on"
    bl_description = "Change how this add-on is named in the organizer (the add-on itself is not touched)"
    bl_options = {'INTERNAL'}

    module: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    new_name: StringProperty(
        name="Name",
        description="Display name shown in the organizer",
        maxlen=storage.MAX_ADDON_NAME_LEN,
        options={'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        entry = addons.find_entry(self.module)
        if entry is None:
            self.report({'ERROR'}, "Add-on not found. Try Refresh from the menu.")
            return {'CANCELLED'}
        self.new_name = storage.display_name(self.module, entry.name)
        return context.window_manager.invoke_props_dialog(
            self, width=360, title="Rename Add-on", confirm_text="Rename")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "new_name", text="")
        entry = addons.find_entry(self.module)
        col = layout.column(align=True)
        col.active = False
        if entry is not None:
            col.label(text="Original name: " + entry.name, translate=False)
        col.label(text="Only changes the name shown here.")
        col.label(text="Leave empty to restore the original.")

    def execute(self, context):
        entry = addons.find_entry(self.module)
        if entry is None:
            self.report({'ERROR'}, "Add-on not found. Try Refresh from the menu.")
            return {'CANCELLED'}
        storage.set_custom_name(self.module, self.new_name, original=entry.name)
        _report_save_problem(self)
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_reset_name(Operator):
    bl_idname = IDNAME_PREFIX + ".reset_name"
    bl_label = "Restore Original Name"
    bl_description = "Show this add-on under its original name again"
    bl_options = {'INTERNAL'}

    module: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        storage.set_custom_name(self.module, "")
        _report_save_problem(self)
        _tag_redraw(context)
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Categories


class ADDONCOVE_OT_assign_category(Operator):
    bl_idname = IDNAME_PREFIX + ".assign_category"
    bl_label = "Move to Category"
    bl_description = "Move this add-on to a category"
    bl_options = {'INTERNAL'}

    module: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    category: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        storage.assign(self.module, self.category)
        _report_save_problem(self)
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_category_new(Operator):
    bl_idname = IDNAME_PREFIX + ".category_new"
    bl_label = "New Category"
    bl_description = "Create a new category"
    bl_options = {'INTERNAL'}

    name: StringProperty(
        name="Name",
        maxlen=storage.MAX_CATEGORY_NAME_LEN,
        options={'SKIP_SAVE'},
    )
    module: StringProperty(
        description="If set, this add-on is moved into the new category",
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        self.name = ""
        return context.window_manager.invoke_props_dialog(
            self, width=300, title="New Category", confirm_text="Create")

    def draw(self, context):
        self.layout.prop(self, "name", text="")

    def execute(self, context):
        clean, error = storage.add_category(self.name)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        if self.module:
            storage.assign(self.module, clean)
        _report_save_problem(self)
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_category_rename(Operator):
    bl_idname = IDNAME_PREFIX + ".category_rename"
    bl_label = "Rename Category"
    bl_description = "Rename this category"
    bl_options = {'INTERNAL'}

    category: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    new_name: StringProperty(
        name="Name",
        maxlen=storage.MAX_CATEGORY_NAME_LEN,
        options={'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        self.new_name = self.category
        return context.window_manager.invoke_props_dialog(
            self, width=300, title="Rename Category", confirm_text="Rename")

    def draw(self, context):
        self.layout.prop(self, "new_name", text="")

    def execute(self, context):
        _clean, error = storage.rename_category(self.category, self.new_name)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        _report_save_problem(self)
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_category_delete(Operator):
    bl_idname = IDNAME_PREFIX + ".category_delete"
    bl_label = "Delete Category"
    bl_description = "Delete this category. Its add-ons are kept and become uncategorized"
    bl_options = {'INTERNAL'}

    category: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        count = storage.count_in_category(self.category)
        message = "\"{:s}\" will be deleted. ".format(self.category)
        message += ("Its {:d} add-on(s) will become uncategorized. ".format(count) if count else "")
        message += "No add-on is changed."
        return context.window_manager.invoke_confirm(
            self, event, title="Delete Category", message=message, confirm_text="Delete", icon='ERROR')

    def execute(self, context):
        storage.delete_category(self.category)
        _report_save_problem(self)
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_category_move(Operator):
    bl_idname = IDNAME_PREFIX + ".category_move"
    bl_label = "Move Category"
    bl_description = "Change the position of this category"
    bl_options = {'INTERNAL'}

    category: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    direction: EnumProperty(
        items=[('UP', "Up", ""), ('DOWN', "Down", "")],
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    def execute(self, context):
        storage.move_category(self.category, -1 if self.direction == 'UP' else 1)
        _report_save_problem(self)
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_category_toggle(Operator):
    bl_idname = IDNAME_PREFIX + ".category_toggle"
    bl_label = "Expand / Collapse"
    bl_description = "Show or hide the add-ons of this category (Ctrl-click: collapse all others)"
    bl_options = {'INTERNAL'}

    category: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        if event.ctrl:
            # Focus mode: open this category, collapse every other one.
            storage.set_all_collapsed(True)
            storage.set_collapsed(self.category, False)
        else:
            storage.set_collapsed(self.category, not storage.is_collapsed(self.category))
        _tag_redraw(context)
        return {'FINISHED'}

    def execute(self, context):
        storage.set_collapsed(self.category, not storage.is_collapsed(self.category))
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_collapse_all(Operator):
    bl_idname = IDNAME_PREFIX + ".collapse_all"
    bl_label = "Expand / Collapse All"
    bl_description = "Expand or collapse every category"
    bl_options = {'INTERNAL'}

    collapse: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        storage.set_all_collapsed(self.collapse)
        _tag_redraw(context)
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Add-on drop-downs (the add-on's own panels, drawn inside the organizer)


class ADDONCOVE_OT_expand_toggle(Operator):
    bl_idname = IDNAME_PREFIX + ".expand_toggle"
    bl_label = "Open / Close Add-on"
    bl_description = "Show or hide this add-on's panels right here"
    bl_options = {'INTERNAL'}

    module: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        storage.set_expanded(self.module, not storage.is_expanded(self.module))
        _report_save_problem(self)
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_panel_toggle(Operator):
    bl_idname = IDNAME_PREFIX + ".panel_toggle"
    bl_label = "Open / Close Panel"
    bl_description = "Expand or collapse this panel"
    bl_options = {'INTERNAL'}

    key: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    default_open: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        sidebar.toggle_panel(self.key, self.default_open)
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_keep_tab_toggle(Operator):
    bl_idname = IDNAME_PREFIX + ".keep_tab_toggle"
    bl_label = "Keep Its Own Sidebar Tab"
    bl_description = (
        "Show this add-on's own tab in the sidebar again, in addition to its drop-down here. "
        "Useful if an add-on doesn't work well inside the organizer"
    )
    bl_options = {'INTERNAL'}

    module: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        storage.set_keep_tab(self.module, not storage.keeps_tab(self.module))
        _report_save_problem(self)
        addons.enforce_sidebar_policy(force=True)
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_open_addon_prefs(Operator):
    bl_idname = IDNAME_PREFIX + ".open_addon_prefs"
    bl_label = "Add-on Preferences"
    bl_description = "Open this add-on's settings in the Preferences window"
    bl_options = {'INTERNAL'}

    module: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        try:
            bpy.ops.preferences.addon_show(module=self.module)
        except Exception as ex:
            self.report({'WARNING'}, "Could not open the preferences: {:s}".format(str(ex)))
            return {'CANCELLED'}
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# List / filters / settings


class ADDONCOVE_OT_refresh(Operator):
    bl_idname = IDNAME_PREFIX + ".refresh"
    bl_label = "Refresh Add-on List"
    bl_description = "Scan the add-on folders and extension repositories for new or removed add-ons"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        addons.get_entries(refresh=True)
        addons.sync_sidebar_tabs(force=True)
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_clear_filters(Operator):
    bl_idname = IDNAME_PREFIX + ".clear_filters"
    bl_label = "Clear Filters"
    bl_description = "Show every add-on again"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        state = context.window_manager.addoncove
        state.state_filter = 'ALL'
        state.only_sidebar = False
        state.search_text = ""
        _tag_redraw(context)
        return {'FINISHED'}


class ADDONCOVE_OT_open_settings(Operator):
    bl_idname = IDNAME_PREFIX + ".open_settings"
    bl_label = "Organizer Settings"
    bl_description = "Open this add-on's preferences (data folder, import / export)"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        try:
            bpy.ops.preferences.addon_show(module=addons.OWN_MODULE)
        except Exception as ex:
            self.report({'WARNING'}, "Could not open the preferences: {:s}".format(str(ex)))
            return {'CANCELLED'}
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Popup menus.  Built from plain functions so they can use the data of the
# clicked row directly (no hidden global "current add-on" state needed).


def draw_item_menu(layout, module: str) -> None:
    current = storage.category_of(module)

    # This action opens a properties dialog. Explicitly use INVOKE_DEFAULT so
    # the popup does not run execute() directly with an empty/default name.
    rename_row = layout.row()
    rename_row.operator_context = 'INVOKE_DEFAULT'
    rename_row.operator(
        IDNAME_PREFIX + ".rename", text="Rename\u2026", icon='GREASEPENCIL',
    ).module = module
    if storage.custom_name(module):
        layout.operator(
            IDNAME_PREFIX + ".reset_name", text="Restore Original Name", icon='LOOP_BACK',
        ).module = module

    if addons.has_preferences(module):
        layout.operator(
            IDNAME_PREFIX + ".open_addon_prefs", text="Add-on Preferences\u2026", icon='PREFERENCES',
        ).module = module
    if sidebar.has_panels(module):
        layout.operator(
            IDNAME_PREFIX + ".keep_tab_toggle", text="Keep Its Own Sidebar Tab",
            icon='CHECKBOX_HLT' if storage.keeps_tab(module) else 'CHECKBOX_DEHLT',
        ).module = module

    layout.separator()
    row = layout.row()
    row.operator_context = 'INVOKE_DEFAULT'
    row.operator(
        IDNAME_PREFIX + ".uninstall", text="Uninstall Add-on...", icon='TRASH',
    ).module = module

    layout.separator()
    layout.label(text="Move to Category")
    for name in storage.categories():
        op = layout.operator(
            IDNAME_PREFIX + ".assign_category", text=name,
            icon='RADIOBUT_ON' if name == current else 'RADIOBUT_OFF', translate=False)
        op.module, op.category = module, name
    op = layout.operator(
        IDNAME_PREFIX + ".assign_category", text=storage.UNCATEGORIZED_LABEL,
        icon='RADIOBUT_ON' if current == storage.UNCATEGORIZED else 'RADIOBUT_OFF')
    op.module, op.category = module, storage.UNCATEGORIZED



def draw_category_menu(layout, category: str) -> None:
    names = storage.categories()
    index = names.index(category) if category in names else -1

    layout.operator(IDNAME_PREFIX + ".category_rename", text="Rename\u2026", icon='GREASEPENCIL').category = category
    row = layout.row()
    row.enabled = index > 0
    op = row.operator(IDNAME_PREFIX + ".category_move", text="Move Up", icon='TRIA_UP')
    op.category, op.direction = category, 'UP'
    row = layout.row()
    row.enabled = 0 <= index < len(names) - 1
    op = row.operator(IDNAME_PREFIX + ".category_move", text="Move Down", icon='TRIA_DOWN')
    op.category, op.direction = category, 'DOWN'
    layout.separator()
    layout.operator(IDNAME_PREFIX + ".category_delete", text="Delete\u2026", icon='TRASH').category = category


def draw_main_menu(layout) -> None:
    layout.operator(IDNAME_PREFIX + ".category_new", text="New Category\u2026", icon='ADD')
    layout.separator()
    layout.operator(
        IDNAME_PREFIX + ".collapse_all", text="Expand All", icon='DISCLOSURE_TRI_DOWN').collapse = False
    layout.operator(
        IDNAME_PREFIX + ".collapse_all", text="Collapse All", icon='DISCLOSURE_TRI_RIGHT').collapse = True
    layout.separator()
    prefs = addons.get_preferences()
    if prefs is not None:
        layout.prop(prefs, "hide_native_tabs")
        layout.separator()
    layout.operator(IDNAME_PREFIX + ".refresh", text="Refresh Add-on List", icon='FILE_REFRESH')
    layout.operator(IDNAME_PREFIX + ".open_settings", text="Organizer Settings\u2026", icon='PREFERENCES')


def _popup(context, draw_body, title: str = "") -> None:
    def draw(self, _context):
        draw_body(self.layout)
    context.window_manager.popup_menu(draw, title=title)


class ADDONCOVE_OT_item_menu(Operator):
    bl_idname = IDNAME_PREFIX + ".item_menu"
    bl_label = "Add-on Options"
    bl_description = "Rename this add-on or move it to another category"
    bl_options = {'INTERNAL'}

    module: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        module = self.module
        _popup(context, lambda layout: draw_item_menu(layout, module), title=shown_name(module))
        return {'FINISHED'}


class ADDONCOVE_OT_category_menu(Operator):
    bl_idname = IDNAME_PREFIX + ".category_menu"
    bl_label = "Category Options"
    bl_description = "Rename, reorder or delete this category"
    bl_options = {'INTERNAL'}

    category: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        category = self.category
        _popup(context, lambda layout: draw_category_menu(layout, category), title=category)
        return {'FINISHED'}


class ADDONCOVE_OT_main_menu(Operator):
    bl_idname = IDNAME_PREFIX + ".main_menu"
    bl_label = "Organizer Menu"
    bl_description = "More options: new category, expand / collapse all, refresh, settings"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        _popup(context, draw_main_menu, title="AddonCove")
        return {'FINISHED'}


# -----------------------------------------------------------------------------

classes = (
    ADDONCOVE_OT_toggle,
    ADDONCOVE_OT_all_toggle,
    ADDONCOVE_OT_install,
    ADDONCOVE_OT_uninstall,
    ADDONCOVE_OT_rename,
    ADDONCOVE_OT_reset_name,
    ADDONCOVE_OT_assign_category,
    ADDONCOVE_OT_category_new,
    ADDONCOVE_OT_category_rename,
    ADDONCOVE_OT_category_delete,
    ADDONCOVE_OT_category_move,
    ADDONCOVE_OT_category_toggle,
    ADDONCOVE_OT_collapse_all,
    ADDONCOVE_OT_expand_toggle,
    ADDONCOVE_OT_panel_toggle,
    ADDONCOVE_OT_keep_tab_toggle,
    ADDONCOVE_OT_open_addon_prefs,
    ADDONCOVE_OT_refresh,
    ADDONCOVE_OT_clear_filters,
    ADDONCOVE_OT_open_settings,
    ADDONCOVE_OT_item_menu,
    ADDONCOVE_OT_category_menu,
    ADDONCOVE_OT_main_menu,
)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass  # was never registered (partial register)