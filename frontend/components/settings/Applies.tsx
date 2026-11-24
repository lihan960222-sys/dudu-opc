import { useT } from "@/lib/use-i18n";

export function Applies({ when }: { when: string }) {
  const t = useT();
  const normalized = when.trim().toLocaleLowerCase();
  const label = normalized === "immediately" || normalized === "now"
    ? t("generalUi.appliesNow")
    : normalized === "next_run" || normalized === "next turn"
      ? t("generalUi.appliesNext")
      : normalized === "restart" || normalized === "after restart"
        ? t("generalUi.appliesRestart")
        : t("generalUi.appliesOther", {when});

  return <span className="rounded bg-inset px-2 py-1 text-xs text-muted">{label}</span>;
}
