"use client";
import { useT } from "@/lib/use-i18n";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { Button, Card, ErrorText } from "./Card";
import { Input } from "@/components/ui/Input";

export function ConnectClaude({
  userId,
  onReady,
  onSandbox,
}: {
  userId: string;
  // Claude is signed in inside the sandbox: tasks can be submitted.
  onReady: (boxId: string) => void;
  // The sandbox exists (Claude may not be signed in yet): enough for GitHub login.
  onSandbox?: (boxId: string) => void;
}) {
  const t = useT();
  const [boxId, setBoxId] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [loginUrl, setLoginUrl] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [status, setStatus] = useState<
    "loading" | "idle" | "starting" | "submitting"
  >("loading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      setStatus("loading");
      setError(null);
      try {
        const sandbox = await api.ensureSandbox(userId);
        if (cancelled) return;
        setBoxId(sandbox.box_id);
        onSandbox?.(sandbox.box_id);

        const authStatus = await api.getClaudeStatus(sandbox.box_id);
        if (cancelled) return;
        setConnected(authStatus.connected);
        if (authStatus.connected) onReady(sandbox.box_id);
        setStatus("idle");
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? `Could not set up sandbox. ${err.message}` : t("settingsPage.couldNotSetUpSandbox"));
        setStatus("idle");
      }
    })();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  const startLogin = async () => {
    if (!boxId) return;
    setError(null);
    setStatus("starting");
    try {
      const { login_url } = await api.startClaudeLogin(boxId);
      setLoginUrl(login_url);
      setStatus("idle");
    } catch (err) {
      setError(err instanceof ApiError ? `Could not start login. ${err.message}` : t("settingsPage.couldNotStartLogin"));
      setStatus("idle");
    }
  };

  const submitCode = async () => {
    if (!boxId || !code.trim()) return;
    setError(null);
    setStatus("submitting");
    try {
      await api.submitClaudeCode(boxId, code.trim());
      setConnected(true);
      setLoginUrl(null);
      onReady(boxId);
    } catch (err) {
      setError(err instanceof ApiError ? `Login failed. ${err.message}` : t("settingsPage.loginFailed"));
    } finally {
      setStatus("idle");
    }
  };

  if (status === "loading") {
    return (
      <Card step={2} title={t("settingsPage.connectClaude")}>
        <p className="text-sm text-muted">{t("settingsPage.settingUpYourSandbox")}</p>
      </Card>
    );
  }

  if (connected) {
    return (
      <Card step={2} title={t("settingsPage.connectClaude")} done>
        <p className="text-sm text-muted">
          {t("settingsPage.claudeIsConnectedForThisAccount")}
        </p>
      </Card>
    );
  }

  return (
    <Card step={2} title={t("settingsPage.connectClaude")}>
      {!loginUrl && (
        <Button onClick={startLogin} disabled={status === "starting" || !boxId}>
          {status === "starting" ? t("settingsPage.starting") : t("settingsPage.startLogin")}
        </Button>
      )}

      {loginUrl && (
        <div className="space-y-3">
          <a
            href={loginUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="block text-sm font-medium text-accent underline hover:opacity-80"
          >
            {t("settingsPage.openClaudeLogin")}
          </a>
          <p className="text-sm text-muted">
            {t("settingsPage.afterLoggingInPasteTheCodeItGives")}
          </p>
          <div className="flex gap-2">
            <Input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder={t("settingsPage.pasteCodeHere")}
              aria-label={t("settingsPage.claudeLoginCode")}
              className="max-w-xs"
            />
            <Button onClick={submitCode} disabled={status === "submitting" || !code.trim()}>
              {status === "submitting" ? t("settingsPage.submitting") : t("settingsPage.submit")}
            </Button>
          </div>
        </div>
      )}

      <ErrorText message={error} />
    </Card>
  );
}
