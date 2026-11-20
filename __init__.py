# SPDX-License-Identifier: GPL-3.0-or-later
"""AddonCove.

Adds a single tab to the 3D Viewport sidebar (N-Panel) to rename, categorize
and enable / disable the installed add-ons and extensions.  The sidebar panels
of other add-ons are hidden and can be opened from each add-on's drop-down
instead.  Names and categories are stored in the organizer's own data file;
managed add-on files are unchanged unless you explicitly uninstall them.
"""

__version__ = (1, 0, 0)

if "bpy" in locals():
    # "Reload Scripts" during development: reload the sub-modules too.
    import importlib
    for _module in (storage, sidebar, addons, operators, preferences, ui):  # noqa: F821
        importlib.reload(_module)
else:
    from . import storage, sidebar, addons, operators, preferences, ui

import bpy  # noqa: E402,F401  (after the reload guard on purpose)


def register():
    storage.load()
    try:
        preferences.register()
        operators.register()
        ui.register()
    except Exception:
        unregister()  # don't leave half of the classes registered
        raise
    addons.start()


def unregister():
    addons.stop()
    ui.unregister()
    operators.unregister()
    preferences.unregister()