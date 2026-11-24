"use client";
import { useT } from "@/lib/use-i18n";
import { settingsTerm } from "@/lib/locales/settings-pages";

import { useId } from "react";
import {
  ACCESS_PRESETS,
  DEFAULT_MEMBER_PERMISSIONS,
  PERMISSION_INFO,
  presetOf,
  togglePermission,
  type MemberRole,
  type Permission,
} from "@/lib/permissions";
import { Checkbox } from "@/components/ui/Checkbox";
import { Select } from "@/components/ui/Input";

export type Access = { role: MemberRole; permissions: Permission[] };

export const DEFAULT_ACCESS: Access = { role: "member", permissions: [...DEFAULT_MEMBER_PERMISSIONS] };

// Chooses what someone may do: a preset (Member, Reply only, Viewer, Admin), and for a member the
// individual permissions. Only the owner can hand out admin and standing rules; the server enforces that,
// this just does not offer what would be refused.
export function AccessPicker({ value, onChange, isOwner, disabled }: {
  value: Access;
  onChange: (next: Access) => void;
  isOwner: boolean;
  disabled?: boolean;
}) {
  const t = useT();
  const id = useId();
  const preset = presetOf(value.role, value.permissions);
  const presets = ACCESS_PRESETS.filter((p) => p.id !== "admin" || isOwner || value.role === "admin");
  const current = ACCESS_PRESETS.find((p) => p.id === preset);

  return (
    <div className="space-y-2">
      <label htmlFor={`${id}-preset`} className="block text-sm font-medium">{t("settingsPage.access")}</label>
      <Select id={`${id}-preset`} value={preset} disabled={disabled}
        onChange={(e) => {
          const chosen = ACCESS_PRESETS.find((p) => p.id === e.target.value);
          if (chosen) onChange({ role: chosen.role, permissions: [...chosen.permissions] });
        }}>
        {presets.map((p) => <option key={p.id} value={p.id}>{settingsTerm(t,p.label)}</option>)}
        {preset === "custom" && <option value="custom">{t("settingsPage.custom")}</option>}
      </Select>
      <p className="text-xs text-muted">{current ? settingsTerm(t,current.help) : t("settingsPage.aMixChosenBelow")}</p>
      {value.role === "member" && (
        <fieldset className="space-y-1.5 rounded-md border border-line p-3" disabled={disabled}>
          <legend className="px-1 text-xs font-medium text-muted">{t("settingsPage.whatTheyMayDo")}</legend>
          {PERMISSION_INFO.map((p) => (
            <Checkbox key={p.id} label={settingsTerm(t,p.label)} description={settingsTerm(t,p.help)}
              checked={value.permissions.includes(p.id)}
              disabled={p.id === "rules" && !isOwner && !value.permissions.includes("rules")}
              onChange={() => onChange({ ...value, permissions: togglePermission(value.permissions, p.id) })} />
          ))}
        </fieldset>
      )}
    </div>
  );
}
