"use client";

import { useT } from "@/lib/use-i18n";

export function TeamWorkspaceAccess({ value, disabled, onChange }: {
  value: boolean; disabled?: boolean; onChange: (value: boolean) => void;
}) {
  const t = useT();
  return (
    <label className="flex flex-wrap items-center justify-between gap-2 text-sm">
      <span>{t("emp.teamWorkspaceAccess")}</span>
      <select className="dp-input w-auto max-w-full rounded-md border border-line bg-surface px-2 py-1"
        value={value ? "read" : "none"} disabled={disabled} onChange={event => onChange(event.target.value === "read")}>
        <option value="none">{t("emp.teamFoldersDenied")}</option>
        <option value="read">{t("emp.teamFoldersRead")}</option>
      </select>
    </label>
  );
}
