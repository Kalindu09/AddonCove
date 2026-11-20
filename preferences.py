# SPDX-License-Identifier: GPL-3.0-or-later
"""Add-on preferences: display options, data folder, backup (export / import)."""

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import AddonPreferences, Operator
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import addons, operators, storage

OP = operators.IDNAME_PREFIX


def _on_hide_changed(self, context):
    # Re-registering panels must not happen inside a property callback.
    addons.request_enforce()


class ADDONCOVE_Preferences(AddonPreferences):
    bl_idname = __package__

    hide_native_tabs: BoolProperty(
        name="Hide Other Add-ons' Sidebar Tabs",
        description=(
            "Remove other add-ons' tabs from the 3D Viewport sidebar so only the organizer is left; "
            "their panels are available from each add-on's drop-down here. "
            "Turn off to get every tab back (nothing is changed on disk either way)"
        ),
        default=True,
        update=_on_hide_changed,
    )

    wrap_names: BoolProperty(
        name="Wrap Long Names",
        description=(
            "Continue long add-on names on a second line so they are never cut off. "
            "Turn off for strictly one line per add-on"
        ),
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        intro = layout.box()
        intro.label(text="AddonCove", icon='PREFERENCES')
        intro.label(text="Manage how add-on panels appear in the 3D Viewport.", icon='INFO')

        display = layout.box()
        display.label(text="Sidebar Behavior", icon='MENU_PANEL')
        display.prop(self, "hide_native_tabs")
        display.prop(self, "wrap_names")

        data = layout.box()
        data.label(text="Data & Backup", icon='FILE_FOLDER')
        path = storage.data_path()
        if path is not None:
            data.label(text=str(path), translate=False)
        else:
            data.alert = True
            data.label(text="No writable user folder found; changes are not saved.", icon='ERROR')
        if storage.last_error():
            data.alert = True
            data.label(text=storage.last_error(), icon='ERROR', translate=False)

        actions = data.row(align=True)
        actions.operator(OP + ".open_data_folder", text="Open Folder", icon='FILE_FOLDER')
        actions.operator(OP + ".export_data", text="Export", icon='EXPORT')
        actions.operator(OP + ".import_data", text="Import", icon='IMPORT')


class ADDONCOVE_OT_open_data_folder(Operator):
    bl_idname = OP + ".open_data_folder"
    bl_label = "Open Folder"
    bl_description = "Open the folder where the organizer keeps its data file"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        directory = storage.data_dir()
        if directory is None:
            self.report({'ERROR'}, "No writable user folder is available.")
            return {'CANCELLED'}
        try:
            bpy.ops.wm.path_open(filepath=str(directory))
        except Exception as ex:
            self.report({'WARNING'}, "Could not open the folder: {:s}".format(str(ex)))
            return {'CANCELLED'}
        return {'FINISHED'}


class ADDONCOVE_OT_export_data(Operator, ExportHelper):
    bl_idname = OP + ".export_data"
    bl_label = "Export"
    bl_description = "Save your custom names and categories to a file (backup, or to use in another Blender version)"
    bl_options = {'INTERNAL'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
        self.filepath = "addoncove_backup.json"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        ok, message = storage.export_to(self.filepath)
        self.report({'INFO'} if ok else {'ERROR'}, message)
        return {'FINISHED'} if ok else {'CANCELLED'}


class ADDONCOVE_OT_import_data(Operator, ImportHelper):
    bl_idname = OP + ".import_data"
    bl_label = "Import"
    bl_description = "Replace your custom names and categories with a previously exported file"
    bl_options = {'INTERNAL'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def execute(self, context):
        ok, message = storage.import_from(self.filepath)
        self.report({'INFO'} if ok else {'ERROR'}, message)
        if ok:
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()
        return {'FINISHED'} if ok else {'CANCELLED'}


classes = (
    ADDONCOVE_Preferences,
    ADDONCOVE_OT_open_data_folder,
    ADDONCOVE_OT_export_data,
    ADDONCOVE_OT_import_data,
)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass