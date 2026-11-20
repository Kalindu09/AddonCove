# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent data for AddonCove.

Only *organizer* data is stored here: custom display names, categories,
category assignments and a few UI memories.  Everything lives in one small
JSON file inside this add-on's own user directory.  Files that belong to the
add-ons being managed are never modified, moved or deleted.

The data is kept in memory and written back (atomically) after every change.
A damaged file is renamed out of the way instead of being overwritten, so a
bad edit can never silently destroy a good backup.
"""

import json
import os
import shutil
import time
from pathlib import Path

import bpy

DATA_VERSION = 1
DATA_FILENAME = "organizer_data.json"

# Internal key of the implicit "Uncategorized" group.  It can never collide
# with a user category because it is rejected as a category name.
UNCATEGORIZED = "__uncategorized__"
UNCATEGORIZED_LABEL = "Uncategorized"

MAX_ADDON_NAME_LEN = 120
MAX_CATEGORY_NAME_LEN = 40
_MAX_TABS_PER_ADDON = 16

_RESERVED_CATEGORY_NAMES = {UNCATEGORIZED, UNCATEGORIZED_LABEL.casefold()}


def _blank() -> dict:
    return {
        "version": DATA_VERSION,
        "categories": [],    # ordered list of category names
        "names": {},         # add-on module -> custom display name
        "assignments": {},   # add-on module -> category name
        "collapsed": [],     # category names (or UNCATEGORIZED) shown collapsed
        "sidebar_tabs": {},  # add-on module -> N-Panel tab names last seen
        "expanded": [],      # add-on modules whose drop-down is open
        "keep_tabs": [],     # add-on modules that keep their own sidebar tab
    }


_data: dict = _blank()
_last_error = ""
_dir_override: Path | None = None  # used by the test-suite only


# -----------------------------------------------------------------------------
# Location


def data_dir() -> Path | None:
    """Return the (created) directory that holds the data file, or None."""
    if _dir_override is not None:
        _dir_override.mkdir(parents=True, exist_ok=True)
        return _dir_override

    path = ""
    package = __package__ or ""
    if package.startswith("bl_ext."):
        # Official per-extension user directory: survives updates/re-installs.
        try:
            path = bpy.utils.extension_path_user(package, create=True)
        except Exception:
            path = ""
    if not path:
        # Fallback (e.g. when run as a plain add-on / from the Text Editor).
        try:
            path = bpy.utils.user_resource('CONFIG', path="addoncove", create=True)
        except Exception:
            path = ""
    return Path(path) if path else None


def data_path() -> Path | None:
    directory = data_dir()
    return directory / DATA_FILENAME if directory else None


def last_error() -> str:
    """Human readable description of the last load/save problem ('' if none)."""
    return _last_error


# -----------------------------------------------------------------------------
# Sanitizing (the file may be hand-edited, truncated or from a newer version)


def _clean_text(value, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_len].rstrip()


def _sanitize(raw) -> dict:
    data = _blank()
    if not isinstance(raw, dict):
        return data

    seen = set()
    categories = raw.get("categories")
    for item in categories if isinstance(categories, list) else ():
        name = _clean_text(item, MAX_CATEGORY_NAME_LEN)
        folded = name.casefold()
        if name and folded not in seen and folded not in _RESERVED_CATEGORY_NAMES:
            seen.add(folded)
            data["categories"].append(name)

    names = raw.get("names")
    for module, name in names.items() if isinstance(names, dict) else ():
        clean = _clean_text(name, MAX_ADDON_NAME_LEN)
        if isinstance(module, str) and module and clean:
            data["names"][module] = clean

    assignments = raw.get("assignments")
    for module, category in assignments.items() if isinstance(assignments, dict) else ():
        if isinstance(module, str) and module and category in data["categories"]:
            data["assignments"][module] = category

    collapsed = raw.get("collapsed")
    for item in collapsed if isinstance(collapsed, list) else ():
        if (item in data["categories"] or item == UNCATEGORIZED) and item not in data["collapsed"]:
            data["collapsed"].append(item)

    tabs = raw.get("sidebar_tabs")
    for module, tab_names in tabs.items() if isinstance(tabs, dict) else ():
        if not (isinstance(module, str) and module and isinstance(tab_names, list)):
            continue
        clean_tabs = [t for t in (_clean_text(t, 64) for t in tab_names) if t]
        if clean_tabs:
            data["sidebar_tabs"][module] = clean_tabs[:_MAX_TABS_PER_ADDON]

    for key in ("expanded", "keep_tabs"):
        values = raw.get(key)
        for item in values if isinstance(values, list) else ():
            if isinstance(item, str) and item and item not in data[key]:
                data[key].append(item)

    return data


# -----------------------------------------------------------------------------
# Load / save


def _previous_data_paths() -> list[Path]:
    """Read-only locations used by the former Add-on Organizer builds.

    The prior settings file is never deleted or modified. A migration only
    happens when AddonCove has no settings file of its own yet.
    """
    candidates = []
    package = __package__ or ""
    if package.startswith("bl_ext."):
        repo_prefix = package.rpartition(".")[0]
        try:
            legacy_path = bpy.utils.extension_path_user(
                repo_prefix + ".addon_organizer", create=False)
            if legacy_path:
                candidates.append(Path(legacy_path) / DATA_FILENAME)
        except Exception:
            pass
    try:
        old_config = bpy.utils.user_resource('CONFIG', path="addon_organizer", create=False)
        if old_config:
            candidates.append(Path(old_config) / DATA_FILENAME)
    except Exception:
        pass
    return candidates


def _migrate_previous_settings(target: Path) -> bool:
    """Copy validated old settings to the new location, leaving originals untouched."""
    if _dir_override is not None:
        return False
    for old_path in _previous_data_paths():
        try:
            if old_path.resolve() == target.resolve() or not old_path.is_file():
                continue
            with old_path.open("r", encoding="utf-8") as stream:
                previous = json.load(stream)
            if not isinstance(previous, dict):
                continue
            global _data
            _data = _sanitize(previous)
            if save():
                return True
        except (OSError, ValueError):
            continue
    return False


def load() -> None:
    """(Re)load the data file.  Never raises."""
    global _data, _last_error
    _data = _blank()
    _last_error = ""

    path = data_path()
    if path is None:
        return
    if not path.is_file():
        _migrate_previous_settings(path)
        return

    try:
        with open(path, "r", encoding="utf-8") as fh:
            _data = _sanitize(json.load(fh))
    except (OSError, ValueError) as ex:
        # Keep the unreadable file for inspection and start fresh.
        backup = path.with_name("{:s}.corrupt-{:d}{:s}".format(path.stem, int(time.time()), path.suffix))
        try:
            os.replace(path, backup)
            _last_error = "Could not read {:s} ({:s}); moved it to {:s}".format(path.name, str(ex), backup.name)
        except OSError:
            _last_error = "Could not read {:s} ({:s})".format(path.name, str(ex))
        print("AddonCove:", _last_error)


def save() -> bool:
    """Write the data file atomically.  Returns False (and records why) on failure."""
    global _last_error
    path = data_path()
    if path is None:
        _last_error = "No writable user directory is available; changes will be lost on restart."
        return False

    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(_data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as ex:
        _last_error = "Could not save {:s}: {:s}".format(path.name, str(ex))
        print("AddonCove:", _last_error)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False

    _last_error = ""
    return True


def _flush_timer():
    save()
    return None  # run once


def schedule_save() -> None:
    """Save shortly from a timer (used from draw code, which must not do I/O)."""
    if not bpy.app.timers.is_registered(_flush_timer):
        bpy.app.timers.register(_flush_timer, first_interval=0.5, persistent=True)


def flush_scheduled_save() -> None:
    """Write now if a timer-based save is still pending (used when shutting down)."""
    if bpy.app.timers.is_registered(_flush_timer):
        bpy.app.timers.unregister(_flush_timer)
        save()


# -----------------------------------------------------------------------------
# Custom display names


def custom_name(module: str) -> str:
    return _data["names"].get(module, "")


def display_name(module: str, original: str) -> str:
    return _data["names"].get(module) or original


def set_custom_name(module: str, name: str, original: str = "") -> bool:
    """Set (or clear, if empty / identical to the original) a display name."""
    clean = _clean_text(name, MAX_ADDON_NAME_LEN)
    if not clean or clean == original:
        _data["names"].pop(module, None)
    else:
        _data["names"][module] = clean
    return save()


# -----------------------------------------------------------------------------
# Categories


def categories() -> list[str]:
    return list(_data["categories"])


def category_of(module: str) -> str:
    """The category name of an add-on, or UNCATEGORIZED."""
    category = _data["assignments"].get(module)
    return category if category in _data["categories"] else UNCATEGORIZED


def check_category_name(name: str, current: str | None = None) -> tuple[str, str]:
    """Validate a category name.  Returns (clean_name, error); error is '' when valid."""
    clean = _clean_text(name, MAX_CATEGORY_NAME_LEN)
    if not clean:
        return "", "The category name can't be empty."
    folded = clean.casefold()
    if folded in _RESERVED_CATEGORY_NAMES:
        return "", "\"{:s}\" is reserved for add-ons without a category.".format(clean)
    for existing in _data["categories"]:
        if existing != current and existing.casefold() == folded:
            return "", "A category named \"{:s}\" already exists.".format(existing)
    return clean, ""


def add_category(name: str) -> tuple[str, str]:
    clean, error = check_category_name(name)
    if error:
        return "", error
    _data["categories"].append(clean)
    save()
    return clean, ""


def rename_category(old: str, new: str) -> tuple[str, str]:
    if old not in _data["categories"]:
        return "", "That category no longer exists."
    clean, error = check_category_name(new, current=old)
    if error:
        return "", error
    if clean != old:
        index = _data["categories"].index(old)
        _data["categories"][index] = clean
        for module, category in _data["assignments"].items():
            if category == old:
                _data["assignments"][module] = clean
        _data["collapsed"] = [clean if c == old else c for c in _data["collapsed"]]
        save()
    return clean, ""


def delete_category(name: str) -> int:
    """Remove a category.  Its add-ons become uncategorized.  Returns how many moved."""
    if name not in _data["categories"]:
        return 0
    _data["categories"].remove(name)
    moved = [m for m, c in _data["assignments"].items() if c == name]
    for module in moved:
        del _data["assignments"][module]
    if name in _data["collapsed"]:
        _data["collapsed"].remove(name)
    save()
    return len(moved)


def move_category(name: str, delta: int) -> bool:
    """Move a category up (-1) or down (+1) in the list."""
    cats = _data["categories"]
    if name not in cats:
        return False
    index = cats.index(name)
    target = index + delta
    if not 0 <= target < len(cats):
        return False
    cats[index], cats[target] = cats[target], cats[index]
    save()
    return True


def assign(module: str, category: str) -> bool:
    """Assign an add-on to a category (UNCATEGORIZED removes the assignment)."""
    if category in _data["categories"]:
        _data["assignments"][module] = category
    else:
        _data["assignments"].pop(module, None)
    return save()


def count_in_category(name: str) -> int:
    return sum(1 for c in _data["assignments"].values() if c == name)


# -----------------------------------------------------------------------------
# Collapsed sections


def is_collapsed(key: str) -> bool:
    return key in _data["collapsed"]


def set_collapsed(key: str, collapsed: bool) -> None:
    present = key in _data["collapsed"]
    if collapsed and not present:
        _data["collapsed"].append(key)
    elif not collapsed and present:
        _data["collapsed"].remove(key)
    else:
        return
    save()


def set_all_collapsed(collapsed: bool) -> None:
    _data["collapsed"] = [*_data["categories"], UNCATEGORIZED] if collapsed else []
    save()


# -----------------------------------------------------------------------------
# N-Panel (3D Viewport sidebar) tabs, remembered so disabled add-ons keep their tag


def sidebar_tabs(module: str) -> list[str]:
    return list(_data["sidebar_tabs"].get(module, ()))


def update_sidebar_tabs(found: dict[str, list[str]], enabled) -> bool:
    """Record tabs for the enabled add-ons.  Returns True if anything changed.

    Does not save by itself (it is called from draw code); see schedule_save().
    """
    tabs = _data["sidebar_tabs"]
    changed = False
    for module in enabled:
        new = list(found.get(module, ()))
        if new:
            if tabs.get(module) != new:
                tabs[module] = new
                changed = True
        elif module in tabs:
            del tabs[module]
            changed = True
    return changed


# -----------------------------------------------------------------------------
# Drop-downs (open add-ons) and add-ons that keep their own sidebar tab


def is_expanded(module: str) -> bool:
    return module in _data["expanded"]


def set_expanded(module: str, expanded: bool) -> None:
    present = module in _data["expanded"]
    if expanded and not present:
        _data["expanded"].append(module)
    elif not expanded and present:
        _data["expanded"].remove(module)
    else:
        return
    save()


def keeps_tab(module: str) -> bool:
    return module in _data["keep_tabs"]


def keep_tabs() -> list[str]:
    return list(_data["keep_tabs"])


def set_keep_tab(module: str, keep: bool) -> None:
    present = module in _data["keep_tabs"]
    if keep and not present:
        _data["keep_tabs"].append(module)
    elif not keep and present:
        _data["keep_tabs"].remove(module)
    else:
        return
    save()


def remove_addon(module: str) -> bool:
    """Remove orphaned organizer records after a successful uninstall."""
    _data["names"].pop(module, None)
    _data["assignments"].pop(module, None)
    _data["sidebar_tabs"].pop(module, None)
    for key in ("expanded", "keep_tabs"):
        if module in _data[key]:
            _data[key].remove(module)
    return save()


# -----------------------------------------------------------------------------
# Export / import (backup and moving between Blender versions)


def export_to(filepath: str) -> tuple[bool, str]:
    try:
        with open(filepath, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(_data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    except OSError as ex:
        return False, "Could not write the file: {:s}".format(str(ex))
    return True, "Exported to {:s}".format(filepath)


def import_from(filepath: str) -> tuple[bool, str]:
    """Replace the current data with a previously exported file.

    The current data is first copied to ``organizer_data.before_import.json``.
    """
    global _data
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as ex:
        return False, "Could not read the file: {:s}".format(str(ex))
    if not isinstance(raw, dict) or not any(k in raw for k in ("categories", "names", "assignments")):
        return False, "This doesn't look like an AddonCove export."

    current = data_path()
    if current is not None and current.is_file():
        try:
            shutil.copy2(current, current.with_name("organizer_data.before_import.json"))
        except OSError:
            pass

    _data = _sanitize(raw)
    if not save():
        return False, _last_error
    return True, "Imported {:d} categories and {:d} custom names.".format(len(_data["categories"]), len(_data["names"]))