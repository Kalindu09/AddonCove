# AddonCove

**Your Blender add-ons, all in one N-panel.** AddonCove is a community-built Blender add-on manager for organizing, installing, enabling, disabling and uninstalling add-ons and extensions.

> **Status:** v1.0.0 — first public/testing release. The earlier development build was named **Add-on Organizer** (v1.2.2). This is a new public version number, not an older codebase. Syntax and simulated registration checks have been completed, but extensive in-Blender testing is still needed. **Back up your Blender settings before testing.**

## Features

- Browse all installed add-ons from one 3D Viewport N-panel tab.
- Organize add-ons into categories and give them custom display names.
- Enable or disable individual add-ons, plus bulk toggle controls.
- Install add-ons from `.zip` and `.py` files using Blender's native installer.
- Uninstall supported user-installed add-ons with confirmation and native Blender removal operators.
- **N-Panel Only** filter enabled by default when Blender starts and when a new `.blend` is loaded.
- Hide other add-ons' N-panel tabs temporarily and access their panels inside AddonCove.
- Native Blender theme colors, search and refresh options.
- Export and import category/name settings as a JSON backup.

## Compatibility

- **Blender 5.2+** (minimum specified in extension manifest).
- Windows, macOS and Linux: no claim of complete cross-platform testing.

## Installation

1. Download **`addoncove_v1.0.0_blender_install.zip`** from this repository's GitHub **Releases** page. Do not install GitHub's automatic `Source code (zip)` archive as though it were the extension package.
2. In Blender open **Edit → Preferences → Get Extensions → Install from Disk** (menu path may vary by Blender version).
3. Select the downloaded ZIP and enable **AddonCove** if necessary.
4. Open the **3D Viewport → N-panel → AddonCove** tab.

### Moving from Add-on Organizer (development versions)

1. Back up your old settings through **Preferences → Export** in the old add-on if possible.
2. Disable or uninstall the old **Add-on Organizer** version and **restart Blender** before enabling AddonCove. Both can modify N-panel registrations, and should not run simultaneously.
3. Install and enable AddonCove as above.
4. AddonCove attempts to **copy** prior category/name data from standard Add-on Organizer settings locations when it has no own settings yet; it never deletes the older file. If your old data doesn't appear, use **AddonCove → Preferences → Import** with the JSON file you exported.

## How to use

1. Open the **AddonCove** sidebar tab.
2. Search or use the category list to find an add-on.
3. Click the checkbox to enable or disable it.
4. Use the menu beside an add-on for its available controls (including uninstall when supported).
5. Use **Install Add-on** to select a local package.
6. Turn off the N-panel tab-hiding option in preferences when you need other add-ons' original sidebar tabs.

## Warnings and limitations

- The **Uninstall** feature can permanently remove installed add-on code. Keep a backup of add-on installers and user settings.
- Some built-in or protected add-ons cannot be removed.
- Toggle/uninstall behavior may depend on third-party add-ons and Blender versions. No guarantee is made that every add-on is safe to manage or compatible.
- If enabling or unloading an add-on fails, inspect **Window → Toggle System Console** on Windows for the complete error.
- AddonCove is **not affiliated with the Blender Foundation**, and is distributed on GitHub. Official Blender Extensions publishing rules are separate and may prohibit management of third-party add-ons.

## Report bugs

Please include your Blender version, OS, the managed add-on's name, the action you performed, and the full traceback. Reproduce the issue with a clean test Blender profile when possible.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

## Credits

AddonCove was developed with assistance from ChatGPT. Project requirements, feature choices, review and publishing decisions belong to the maintainer. This statement does not imply affiliation with OpenAI or the Blender Foundation.