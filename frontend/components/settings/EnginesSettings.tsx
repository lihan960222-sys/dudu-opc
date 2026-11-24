"use client";
import { translateNow, useT } from "@/lib/use-i18n";
import { settingsTerm } from "@/lib/locales/settings-pages";

import { useEffect, useState } from "react";
import { api, ApiError, isLaunchTokenMissing, type ApplicationSetting, type DesktopSettings } from "@/lib/api";
import { EngineSettings } from "@/components/EngineSettings";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { Input } from "@/components/ui/Input";
import { SkeletonList } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { SettingsCard } from "./SettingsCard";
import { Applies } from "./Applies";
import { SaveBar } from "./SaveBar";
import { SavedConfirmation, useSavedConfirmation } from "./SavedConfirmation";

declare global {
  interface Window {
    suparagent?: {
      desktopPermissions?(action: "status" | "accessibility" | "screen_recording"): Promise<{ supported: boolean; accessibility: boolean; screen_recording: boolean }>;
      choosePath(kind: "file" | "folder"): Promise<string | null>;
      openEngineSignIn?(url: string): Promise<void>;
      openGoogleSignIn(url: string): Promise<void>;
      openComposioLink(url: string): Promise<void>;
      nativeControl(request: import("@/components/chat/NativeComputer").NativeRequest): Promise<import("@/components/chat/NativeComputer").NativeStatus | { held: boolean }>;
      updates?: import("@/components/AppUpdates").UpdatesBridge;
      copyText?(text: string): Promise<boolean>;
    };
  }
}

const fields: Record<string, { label: string; help: string; type: "file" | "folder" | "checkbox" | "number"; min?: number; max?: number }> = {
  LOCAL_CLAUDE_CLI_PATH: { label: "settingsPage.claudeExecutable", help: "settingsPage.leaveEmptyForAutomaticDetectionFromPathA", type: "file" },
  LOCAL_WORKSPACE_ROOT: { label: "settingsPage.workspaceRoot", help: "settingsPage.newConversationsUseAWorkspaceSubfolderHereExisting", type: "folder" },
  LOCAL_TOOLS_ENABLED: { label: "settingsPage.allowLocalTools", help: "settingsPage.allowsFileReadsFileChangesAndCommandsOn", type: "checkbox" },
  LOCAL_TASK_TIMEOUT_SECONDS: { label: "settingsPage.answerTimeoutSeconds", help: "settingsPage.0DisablesTheHardTimeLimitSilentTurns", type: "number", min: 0, max: 3600 },
  LOCAL_APPROVAL_TIMEOUT_SECONDS: { label: "settingsPage.approvalTimeoutSeconds", help: "settingsPage.newApprovalsUseThisLimitQuestionsAlreadyWaiting", type: "number", min: 30, max: 3600 },
  LOCAL_MAX_OUTPUT_BYTES: { label: "settingsPage.answerOutputLimitBytes", help: "settingsPage.boundsTheAnswerTextRetainedFromEachRun", type: "number", min: 1024, max: 16000000 },
};

const settingAnchors: Record<string, string> = {
  LOCAL_CLAUDE_CLI_PATH: "setting-engine-claude-executable",
  LOCAL_WORKSPACE_ROOT: "setting-engine-workspace-root",
  LOCAL_TOOLS_ENABLED: "setting-engine-local-tools",
  LOCAL_TASK_TIMEOUT_SECONDS: "setting-engine-answer-timeout",
  LOCAL_APPROVAL_TIMEOUT_SECONDS: "setting-engine-approval-timeout",
  LOCAL_MAX_OUTPUT_BYTES: "setting-engine-output-limit",
};

export function EnginesSettings() {
  const t = useT();
  const display = (value: unknown) => value === null || value === undefined ? t("settingsPage.unavailable") : value === "" ? t("settingsPage.automaticAppDefault") : typeof value === "string" ? value : JSON.stringify(value);
  const applies = (row: ApplicationSetting) => row.key === "LOCAL_WORKSPACE_ROOT" ? t("settingsPage.newConversations") : row.key === "LOCAL_APPROVAL_TIMEOUT_SECONDS" ? t("settingsPage.newApprovals") : row.applies === "next_run" ? t("settingsPage.nextTurn") : row.applies === "restart" ? t("settingsPage.afterRestart") : t("settingsPage.immediately");


  const [data, setData] = useState<DesktopSettings | null>(null);
  const [drafts, setDrafts] = useState<Record<string, unknown>>({});
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const toast = useToast();
  const confirmation = useSavedConfirmation();
  const [loadTick, setLoadTick] = useState(0);
  const [checking, setChecking] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [headless, setHeadless] = useState(false);
  const settingUp = false;

  useEffect(() => {
    let cancelled = false;
    api.getDesktopSettings().then((value) => {
      if (cancelled) return;
      setData(value);
      setHeadless(false);
      setDrafts(Object.fromEntries(value.settings.map((row) => [row.key, row.value])));
      setError(null);
    }).catch((err: unknown) => {
      if (cancelled) return;
      if (err instanceof ApiError && err.status === 404) { setHeadless(true); setError(null); }
      else setError(isLaunchTokenMissing(err) ? translateNow("settingsPage.openSettingsInsideTheSuparagentAppWindow") : err instanceof ApiError ? err.message : translateNow("settingsPage.couldNotReachTheBackendCheckThatThe"));
    });
    return () => { cancelled = true; };
  }, [loadTick]);

  const updateSavedRow = (row: ApplicationSetting, saved: ApplicationSetting) => {
    setData((previous) => previous && ({ ...previous, settings: previous.settings.map((item) => item.key === row.key ? saved : item) }));
    setDrafts((previous) => ({ ...previous, [row.key]: saved.value }));
  };

  const save = async (row: ApplicationSetting) => {
    setBusy(row.key); setError(null);
    setRowErrors((previous) => ({ ...previous, [row.key]: "" }));
    try {
      const saved = await api.saveDesktopSetting(row.key, drafts[row.key], row.version);
      updateSavedRow(row, saved);
      confirmation.showSaved(row.key);
      toast(t("settingsPage.savedWhen",{field:t(fields[row.key].label),when:applies(saved)}));
      if (row.key === "LOCAL_CLAUDE_CLI_PATH") setStatus(null);
    } catch (err) { setRowErrors((previous) => ({ ...previous, [row.key]: err instanceof ApiError ? err.message : t("settingsPage.couldNotSaveYourChangesRemainInThe") })); }
    finally { setBusy(null); }
  };

  const saveInstant = async (row: ApplicationSetting, value: boolean) => {
    setBusy(row.key); setRowErrors((previous) => ({ ...previous, [row.key]: "" }));
    setDrafts((previous) => ({ ...previous, [row.key]: value }));
    confirmation.clearSaved(row.key);
    try {
      const saved = await api.saveDesktopSetting(row.key, value, row.version);
      updateSavedRow(row, saved);
      confirmation.showSaved(row.key);
      toast(t("settingsPage.savedWhen",{field:t(fields[row.key].label),when:applies(saved)}));
    } catch (err) {
      setDrafts((previous) => ({ ...previous, [row.key]: row.value }));
      setRowErrors((previous) => ({ ...previous, [row.key]: err instanceof ApiError ? err.message : t("settingsPage.couldNotSaveThisChange") }));
    } finally { setBusy(null); }
  };

  const browse = async (row: ApplicationSetting) => {
    setBusy(row.key); setError(null);
    try {
      if (!window.suparagent) throw new Error(t("settingsPage.browseIsAvailableInsideTheDesktopApp"));
      const selected = await window.suparagent.choosePath(fields[row.key].type === "file" ? "file" : "folder");
      if (selected !== null) {
        setDrafts((previous) => ({ ...previous, [row.key]: selected }));
        setRowErrors((previous) => ({ ...previous, [row.key]: "" }));
        confirmation.clearSaved(row.key);
      }
    } catch (err) { setError(err instanceof Error ? err.message : t("settingsPage.couldNotOpenThePicker")); }
    finally { setBusy(null); }
  };

  const check = async () => {
    setChecking(true);
    try {
      const result = await api.testDesktopClaude();
      setStatus(result.available ? `${t("settingsPage.ready")}${result.version ? ` · ${result.version}` : ""}` : result.error || t("settingsPage.claudeIsNotReady"));
    } catch { setStatus(t("settingsPage.couldNotCheckClaudeTryAgain")); }
    finally { setChecking(false); }
  };

  return <div className="space-y-6" data-setting-root>
      <div className="sr-only" aria-label={t("settingsPage.engineSettingAnchors")}>
        <span id="setting-engine-claude-executable" />
        <span id="setting-engine-workspace-root" />
        <span id="setting-engine-local-tools" />
        <span id="setting-engine-answer-timeout" />
        <span id="setting-engine-approval-timeout" />
        <span id="setting-engine-output-limit" />
      </div>
      {error && <Banner tone="danger" action={!headless ? <Button size="sm" onClick={() => setLoadTick((n) => n + 1)}>{t("settingsPage.tryAgain")}</Button> : undefined}>{error}</Banner>}
      {headless && <p className="text-sm text-muted">{t("settingsPage.workspaceExecutableAndTimeoutPreferencesUseTheBackend")}</p>}
      {!data && !error && !headless && <SkeletonList rows={4} />}
      {!headless && data && <Button disabled={busy !== null} onClick={() => setLoadTick((n) => n + 1)}>{t("settingsPage.reloadSavedSettings")}</Button>}
      {data && !settingUp && <>
        <SettingsCard title="Claude Code" className="space-y-5">
          {data.settings.map((row) => {
            const field = fields[row.key];
            if (!field) return null;
            const value = drafts[row.key];
            const dirty = JSON.stringify(value) !== JSON.stringify(row.value);
            const formId = `local-setting-${row.key}`;
            return <form key={row.key} id={formId} data-setting-id={settingAnchors[row.key]} onSubmit={(event) => { event.preventDefault(); void save(row); }} className="space-y-2 border-b border-line pb-5 last:border-0 last:pb-0">
              <label htmlFor={row.key} className="block text-sm font-medium">{t(field.label)}</label>
              <div className="flex items-center gap-2">
                {field.type === "checkbox" ? <Checkbox id={row.key} label={t("settingsPage.on")} disabled={busy !== null} checked={value === true} onChange={(event) => void saveInstant(row, event.target.checked)} /> : <Input id={row.key} disabled={busy !== null} type={field.type === "number" ? "number" : "text"} min={field.min} max={field.max} step={1} required={field.type === "number"} value={field.type === "number" ? typeof value === "number" ? value : "" : typeof value === "string" ? value : ""} onChange={(event) => { setDrafts((previous) => ({ ...previous, [row.key]: field.type === "number" ? event.target.value === "" ? null : Number(event.target.value) : event.target.value })); setRowErrors((previous) => ({ ...previous, [row.key]: "" })); confirmation.clearSaved(row.key); }} autoComplete="off" spellCheck={false} />}
                {(field.type === "file" || field.type === "folder") && <Button aria-label={t("settingsPage.browseField",{field:t(field.label)})} disabled={busy !== null} onClick={() => browse(row)}>{t("settingsPage.browse")}</Button>}
              </div>
              <p className="text-xs text-muted">{t(field.help)}{field.type === "number" ? " " + t("settingsPage.range",{min:field.min!,max:field.max!}) : ""}</p>
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted"><span className="rounded bg-inset px-2 py-1">{t("settingsPage.source")} {settingsTerm(t,row.source)}</span><Applies when={applies(row)} /></div>
              <p className="break-all text-xs text-muted">{t("settingsPage.effective")} {display(row.effective_value)} {t("settingsPage.default")} {display(row.default)}</p>
              {row.resolved_path && <p className="break-all font-mono text-xs text-muted">{t("settingsPage.newConversationFolder")} {row.resolved_path}</p>}
              {row.error && <p role="alert" className="text-xs text-danger">{row.error} {t("settingsPage.storedValue")} {display(row.value)}</p>}
              {field.type === "checkbox" ? <>
                <SavedConfirmation visible={confirmation.saved.has(row.key)} />
                {rowErrors[row.key] && <p role="alert" className="text-xs text-danger">{rowErrors[row.key]}</p>}
              </> : <>
                <SaveBar dirty={dirty} saving={busy === row.key} error={rowErrors[row.key] || null} saved={confirmation.saved.has(row.key)} onSave={() => {
                  const form = document.getElementById(formId);
                  if (form instanceof HTMLFormElement) form.requestSubmit();
                }} onDiscard={() => { setDrafts((previous) => ({ ...previous, [row.key]: row.value })); setRowErrors((previous) => ({ ...previous, [row.key]: "" })); confirmation.clearSaved(row.key); }} />
              </>}
            </form>;
          })}
          <Button loading={checking} disabled={busy !== null} onClick={check}>{checking ? t("settingsPage.checking") : t("settingsPage.checkClaudeStatus")}</Button>
          {status && <p role="status" className="text-sm">{status}</p>}
          <p className="text-xs text-muted">{t("settingsPage.checksSavedSettingsToSignInUseSign")} <code>claude auth login</code> {t("settingsPage.inATerminalThenCheckAgain")}</p>
        </SettingsCard>
        <div id="setting-engine-storage" data-setting-id="setting-engine-storage" data-setting-container tabIndex={-1}>
          <SettingsCard title={t("settingsPage.storage")} description={t("settingsPage.managedByTheDesktopAppTheseLocationsCannot")}><dl className="space-y-3 text-sm">{Object.entries(data.storage).map(([name, value]) => <div key={name}><dt className="font-medium capitalize">{settingsTerm(t,name.replaceAll("_", " "))}</dt><dd className="mt-1 break-all text-xs text-muted">{name === "encryption_key" ? settingsTerm(t,value) : value}</dd></div>)}</dl></SettingsCard>
        </div>
      </>}
      {!settingUp && <div className="space-y-3" data-setting-root data-setting-container data-setting-id="setting-engine-connections" tabIndex={-1}>
        <div className="sr-only" aria-label={t("settingsPage.engineConnectionAnchors")}>
          <span id="setting-engine-connections" />
          <span id="setting-engine-api-key" />
          <span id="setting-engine-endpoint" />
        </div>
        <EngineSettings />
      </div>}
    </div>;
}
