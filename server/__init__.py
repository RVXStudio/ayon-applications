"""Server side of the Applications addon.

This module contains the server side of the Applications addon.
It is responsible for managing settings and initial setup of addon.

## Attributes backward compatibility
Current and previous versions of applications addon did use AYON attributes
to define applications and tools for a project and task.

This system was replaced with a new system using settings. This change is
not 100% backwards compatible, we need to make sure that older versions of
the addon don't break initialization.

Older versions of the addon used settings of other versions, but
the settings structure did change which can cause that combination of old
and new Applications addon on server can cause crashes.

First version introduction settings does support both settings and attributes
so the handling of older versions is part of the addon, but following versions
have to find some clever way how to avoid the issues.

Version stored under 'ATTRIBUTES_VERSION_MILESTONE' should be last released
version that used only old attribute system.
"""
import semver

from ayon_server.addons import BaseServerAddon, AddonLibrary
from ayon_server.entities.core import attribute_library
from ayon_server.lib.postgres import Postgres

from .settings import ApplicationsAddonSettings, DEFAULT_VALUES


ATTRIBUTES_VERSION_MILESTONE = (0, 2, 5)

def parse_version(version):
    try:
        return semver.VersionInfo.parse(version)
    except ValueError:
        return None


def parse_versions(versions):
    version_objs = []
    invalid_versions = []
    output = (version_objs, invalid_versions)
    for version in versions:
        parsed_version = parse_version(version)
        if parsed_version is None:
            invalid_versions.append(version)
        else:
            version_objs.append((version, parsed_version))
    return output


class ApplicationsAddon(BaseServerAddon):
    settings_model = ApplicationsAddonSettings
    # TODO remove this attribute when attributes support is removed
    has_attributes = True

    async def get_default_settings(self):
        return self.get_settings_model()(**DEFAULT_VALUES)

    async def pre_setup(self):
        """Make sure older version of addon use the new way of attributes."""

        instance = AddonLibrary.getinstance()
        app_defs = instance.data.get(self.name)
        old_addon = app_defs.versions.get("0.1.0")
        if old_addon is not None:
            # Override 'create_applications_attribute' for older versions
            #   - avoid infinite server restart loop
            old_addon.create_applications_attribute = (
                self.create_applications_attribute
            )

        # Update older versions of applications addon to use new
        #   '_update_enums'
        # - new function skips newer addon versions without 'has_attributes'
        version_objs, invalid_versions = parse_versions(app_defs.versions)
        for addon_version, version_obj in version_objs:
            # Last release with only old attribute system
            if version_obj > ATTRIBUTES_VERSION_MILESTONE:
                continue
            addon = app_defs.versions[addon_version]
            addon._update_enums = self._update_enums

    # --------------------------------------
    # Backwards compatibility for attributes
    # --------------------------------------
    def _sort_versions(self, addon_versions, reverse=False):
        version_objs, invalid_versions = parse_versions(addon_versions)

        valid_versions = [
            addon_version
            for addon_version, version_obj in (
                sorted(version_objs, key=lambda x: x[1])
            )
        ]
        sorted_versions = list(sorted(invalid_versions)) + valid_versions
        if reverse:
            sorted_versions = reversed(sorted_versions)
        for addon_version in sorted_versions:
            yield addon_version

    def _merge_groups(self, output, new_groups):
        groups_by_name = {
            o_group["name"]: o_group
            for o_group in output
        }
        extend_groups = []
        for new_group in new_groups:
            group_name = new_group["name"]
            if group_name not in groups_by_name:
                extend_groups.append(new_group)
                continue
            existing_group = groups_by_name[group_name]
            existing_variants = existing_group["variants"]
            existing_variants_by_name = {
                variant["name"]: variant
                for variant in existing_variants
            }
            for new_variant in new_group["variants"]:
                if new_variant["name"] not in existing_variants_by_name:
                    existing_variants.append(new_variant)

        output.extend(extend_groups)

    def _get_enum_items_from_groups(self, groups):
        label_by_name = {}
        for group in groups:
            group_name = group["name"]
            group_label = group["label"] or group_name
            for variant in group["variants"]:
                variant_name = variant["name"]
                if not variant_name:
                    continue
                variant_label = variant["label"] or variant_name
                full_name = f"{group_name}/{variant_name}"
                full_label = f"{group_label} {variant_label}"
                label_by_name[full_name] = full_label

        return [
            {"value": full_name, "label": label_by_name[full_name]}
            for full_name in sorted(label_by_name)
        ]

    def _addon_has_attributes(self, addon, addon_version):
        version_obj = parse_version(addon_version)
        if version_obj is None:
            return True
        if version_obj > ATTRIBUTES_VERSION_MILESTONE:
            return getattr(addon, "has_attributes", False)
        return True

    async def _update_enums(self):
        """Updates applications and tools enums based on the addon settings.
        This method is called when the addon is started (after we are sure that the
        'applications' and 'tools' attributes exist) and when the addon settings are
        updated (using on_settings_updated method).
        """

        instance = AddonLibrary.getinstance()
        app_defs = instance.data.get(self.name)
        all_applications = []
        all_tools = []
        for addon_version in self._sort_versions(
            app_defs.versions.keys(), reverse=True
        ):
            addon = app_defs.versions[addon_version]
            if not self._addon_has_attributes(addon, addon_version):
                continue

            for variant in ("production", "staging"):
                settings_model = await addon.get_studio_settings(variant)
                studio_settings = settings_model.dict()
                application_settings = studio_settings["applications"]
                app_groups = application_settings.pop("additional_apps")
                for group_name, value in application_settings.items():
                    value["name"] = group_name
                    app_groups.append(value)
                self._merge_groups(all_applications, app_groups)
                self._merge_groups(all_tools, studio_settings["tool_groups"])

        apps_attrib_name = "applications"
        tools_attrib_name = "tools"

        apps_enum = self._get_enum_items_from_groups(all_applications)
        tools_enum = self._get_enum_items_from_groups(all_tools)

        apps_attribute_data = {
            "type": "list_of_strings",
            "title": "Applications",
            "enum": apps_enum,
        }
        tools_attribute_data = {
            "type": "list_of_strings",
            "title": "Tools",
            "enum": tools_enum,
        }

        apps_scope = ["project"]
        tools_scope = ["project", "folder", "task"]

        apps_matches = False
        tools_matches = False

        async for row in Postgres.iterate(
            "SELECT name, position, scope, data from public.attributes"
        ):
            if row["name"] == apps_attrib_name:
                # Check if scope is matching ftrack addon requirements
                if (
                    set(row["scope"]) == set(apps_scope)
                    and row["data"].get("enum") == apps_enum
                ):
                    apps_matches = True

            elif row["name"] == tools_attrib_name:
                if (
                    set(row["scope"]) == set(tools_scope)
                    and row["data"].get("enum") == tools_enum
                ):
                    tools_matches = True

        if apps_matches and tools_matches:
            return

        if not apps_matches:
            await Postgres.execute(
                """
                UPDATE attributes SET
                    scope = $1,
                    data = $2
                WHERE 
                    name = $3
                """,
                apps_scope,
                apps_attribute_data,
                apps_attrib_name,
            )

        if not tools_matches:
            await Postgres.execute(
                """
                UPDATE attributes SET
                    scope = $1,
                    data = $2
                WHERE 
                    name = $3
                """,
                tools_scope,
                tools_attribute_data,
                tools_attrib_name,
            )

        # Reset attributes cache on server
        await attribute_library.load()
