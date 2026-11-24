"use client";

import Link from "next/link";
import { useT } from "@/lib/use-i18n";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { Input } from "@/components/ui/Input";
import { EmptyPanel, ErrorState, LoadingState } from "./PanelState";
import {
  categoryLabel,
  filterTemplates,
  requirementBadges,
  type AgentTemplate,
  type TemplateCatalog,
  type TemplateSkill,
} from "@/lib/templates";

const CHIP = "cursor-pointer rounded-full border px-3 py-1 text-xs font-medium";

// Ready-made agents: browse and search a bundled list, then Add one. Add only
// hands the template to the new-agent form; nothing is created here.
export function TemplatesGallery({ onAdd }: { onAdd: (template: AgentTemplate) => void }) {
  const t = useT();
  const [catalog, setCatalog] = useState<TemplateCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .listTemplates()
      .then((value) => { if (!cancelled) setCatalog(value); })
      .catch(() => { if (!cancelled) setError(t("emp.templatesError")); });
    return () => { cancelled = true; };
  }, [reload, t]);

  const shown = useMemo(() => filterTemplates(catalog?.templates ?? [], query, category), [catalog, query, category]);

  if (error) return <ErrorState message={error} onRetry={() => { setError(null); setReload((n) => n + 1); }} />;
  if (!catalog) return <LoadingState rows={4} />;

  return (
    <div className="space-y-3">
      <Input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("emp.searchTemplates")}
        aria-label={t("emp.searchTemplates")}
      />
      <div className="flex flex-wrap gap-2" role="group" aria-label={t("emp.category")}>
        {[null, ...catalog.categories].map((value) => (
          <button
            key={value ?? "all"}
            type="button"
            aria-pressed={category === value}
            onClick={() => setCategory(value)}
            className={`${CHIP} ${category === value ? "border-accent bg-inset" : "border-line hover:bg-hover"}`}
          >
            {value === null ? t("emp.all") : categoryLabel(value)}
          </button>
        ))}
      </div>
      {shown.length === 0 ? (
        <EmptyPanel title={t("emp.noMatches")} description={t("emp.tryCategory")} />
      ) : (
        <ul className="grid min-w-0 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {shown.map((template) => (
            <li key={template.id} className="flex min-w-0 flex-col rounded-md border border-line p-3">
              <div className="flex items-center gap-2">
                <span
                  aria-hidden
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold text-accent-fg"
                  style={{ backgroundColor: template.color }}
                >
                  {template.name.charAt(0).toUpperCase()}
                </span>
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{template.name}</p>
                  <p className="truncate text-xs text-muted">{template.role} · {categoryLabel(template.category)}</p>
                </div>
              </div>
              <p className="mt-2 text-xs">{template.description}</p>
              {template.example_requests[0] && (
                <p className="mt-2 break-words text-xs italic text-muted">“{template.example_requests[0]}”</p>
              )}
              <div className="mt-2 flex flex-wrap gap-1">
                {requirementBadges(template).map((badge) =>
                  badge.connectHref ? (
                    <Link
                      key={badge.requirement}
                      href={badge.connectHref}
                      className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted underline hover:bg-hover"
                    >
                      {badge.text}
                    </Link>
                  ) : (
                    <span key={badge.requirement} className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted">
                      {badge.text}
                    </span>
                  ),
                )}
                {template.skills.length > 0 && (
                  <span className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted">
                    {t("emp.skillCount", {count:template.skills.length})}
                  </span>
                )}
                {template.routines.length > 0 && (
                  <span className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted">
                    {t("emp.routineCount", {count:template.routines.length})}
                  </span>
                )}
                {template.routines.length > 0 && (
                  <span className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted">
                    {t(`emp.depth.${template.setup_depth}`)}
                  </span>
                )}
              </div>
              <div className="mt-auto pt-3">
                <button
                  type="button"
                  onClick={() => onAdd(template)}
                  disabled={!template.available}
                  aria-label={t("emp.useNamed", {name:template.name})}
                  className="cursor-pointer rounded-md border border-line px-3 py-1 text-xs font-medium hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {template.available ? t("emp.useTemplate") : t("header.unavailable")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// The skills a template suggests, each optional, shown in the new-agent form.
export function TemplateSkillChoices({
  skills,
  chosen,
  disabled,
  onChange,
}: {
  skills: TemplateSkill[];
  chosen: string[];
  disabled: boolean;
  onChange: (chosen: string[]) => void;
}) {
  const t = useT();
  return (
    <fieldset className="text-xs text-muted">
      <legend>{t("emp.suggestedSkills")}</legend>
      <ul className="mt-1 space-y-1">
        {skills.map((skill) => (
          <li key={skill.name}>
            <label className="flex cursor-pointer items-start gap-2">
              <input
                type="checkbox"
                disabled={disabled}
                checked={chosen.includes(skill.name)}
                onChange={() => onChange(chosen.includes(skill.name) ? chosen.filter((n) => n !== skill.name) : [...chosen, skill.name])}
              />
              <span className="min-w-0 break-words">
                <span className="font-medium text-foreground">{skill.name}</span> · {skill.description}
              </span>
            </label>
          </li>
        ))}
      </ul>
    </fieldset>
  );
}
