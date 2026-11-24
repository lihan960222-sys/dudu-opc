import { modelLabel } from "./agents.ts";

// A roster describes the saved role. A null model follows the engine; the
// model selected for an individual conversation does not change this display.
export function employeeModelName(model: string | null, roleDefault: string, custom: string) {
  const id = model?.trim();
  if (!id || id === "default") return roleDefault;
  const label = modelLabel(id);
  if (label !== id) return label;
  const claude = /^(?:claude-)?(opus|sonnet|haiku)(?:-(\d+)(?:-(\d+))?)?$/.exec(id);
  if (claude) return `Claude ${claude[1][0].toUpperCase() + claude[1].slice(1)}${claude[2] ? " " + claude[2] : ""}${claude[3] ? "." + claude[3] : ""}`;
  return custom;
}

export function teamActivityLabel(kind: string, t: (key: string) => string): string {
  const key = `teamActivity.${kind}`;
  const label = t(key);
  return label === key ? t("team.activity") : label;
}
