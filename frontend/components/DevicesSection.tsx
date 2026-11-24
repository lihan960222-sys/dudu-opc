"use client";
import { translateNow, useT } from "@/lib/use-i18n";
import { settingsTerm } from "@/lib/locales/settings-pages";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE, ApiError } from "@/lib/api";
import { appPairLink, appServerOrigin } from "@/lib/app-link";
import { auth } from "@/lib/auth";
import { encodeQr, qrSvgPath } from "@/lib/qr";
import { codeAccess, deviceAccess, PAIRING_CHOICES, type PairingScope } from "@/lib/access-label";
import type { Device, PairingCode } from "@/lib/session";
import { useSession } from "./AuthGate";
import { Button } from "@/components/ui/Button";
import { Input, Select } from "@/components/ui/Input";
import { Banner } from "@/components/ui/Banner";
import { SkeletonList } from "@/components/ui/Skeleton";
import { EmptyState } from "@/components/ui/EmptyState";


function when(iso: string): string {
  return new Date(iso).toLocaleString();
}

function QrCode({ text, label, testId = "pairing-qr" }: { text: string; label?: string; testId?: string }) {
  const t = useT();
  const qr = useMemo(() => {
    const matrix = encodeQr(text);
    return matrix ? qrSvgPath(matrix) : null;
  }, [text]);
  if (!qr) return null;
  return (
    <svg role="img" aria-label={label ?? t("settingsPage.pairingQrCode")} viewBox={`0 0 ${qr.size} ${qr.size}`} width={192} height={192}
      shapeRendering="crispEdges" className="rounded bg-white" data-testid={testId}>
      <rect width={qr.size} height={qr.size} fill="#fff" />
      <path d={qr.path} fill="#000" />
    </svg>
  );
}

function OpenCode({ code, onDone }: { code: PairingCode; onDone: () => void }) {
  const t = useT();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const left = Math.max(0, Math.round((new Date(code.expires_at).getTime() - now) / 1000));
  const link = `${window.location.origin}/pair#code=${encodeURIComponent(code.display_code)}`;
  const appLink = appPairLink(appServerOrigin(window.location.origin, API_BASE), code.display_code);
  return (
    <div className="space-y-3 rounded-md border border-line p-4" aria-live="polite">
      {left > 0 ? (
        <>
          <p className="text-sm">{t("settingsPage.onTheOtherDeviceScanTheCodeOr")} <span className="break-all font-mono text-xs">{link}</span></p>
          <div className="flex flex-wrap items-center gap-4">
            <QrCode text={link} />
            <div>
              <p className="font-mono text-2xl font-semibold tracking-widest" data-testid="pairing-code">{code.display_code}</p>
              <p className="text-xs text-muted">{settingsTerm(t,codeAccess(code.scope))} {t("settingsPage.worksOnce")} {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")} {t("settingsPage.left")}</p>
            </div>
          </div>
          {code.scope !== "admin" ? (
            <div className="space-y-2" data-testid="app-pairing">
              <p className="text-sm">{t("settingsPage.inThePhoneAppScanThisCodeInstead")} <span className="break-all font-mono text-xs">{appLink}</span></p>
              <QrCode text={appLink} label={t("settingsPage.phoneAppPairingQrCode")} testId="app-pairing-qr" />
            </div>
          ) : (
            <p className="text-xs text-muted">{t("settingsPage.thePhoneAppDoesNotAcceptAFull")}</p>
          )}
        </>
      ) : (
        <p className="text-sm text-muted">{t("settingsPage.thatCodeHasExpired")}</p>
      )}
      <Button type="button" onClick={onDone}>{t("settingsPage.done")}</Button>
    </div>
  );
}

// Signed-in browsers and paired devices: pair a new one, see and revoke them (docs/AUTH.md).
export function DevicesSection() {
  const t = useT();
  const { session } = useSession();
  const router = useRouter();
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState<PairingScope>("chat");
  const [label, setLabel] = useState("");
  const [code, setCode] = useState<PairingCode | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<number | null>(null);

  const admin = session !== null && session.kind !== "desktop" && session.scopes.includes("admin");

  const load = useCallback(async () => {
    try {
      setDevices(await auth.devices());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? `${translateNow("settingsPage.couldNotLoadDevices")} ${err.message}` : translateNow("settingsPage.couldNotLoadDevices"));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (admin) void load();
  }, [admin, load]);

  if (!session || session.kind === "desktop") return null;

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      setCode(await auth.createPairingCode(scope, label));
      setLabel("");
    } catch (err) {
      setError(err instanceof ApiError ? `${t("settingsPage.couldNotCreateAPairingCode")} ${err.message}` : t("settingsPage.couldNotCreateAPairingCode"));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (device: Device) => {
    if (confirming !== device.id) {
      setConfirming(device.id);
      return;
    }
    setConfirming(null);
    setBusy(true);
    try {
      await auth.revokeDevice(device.id);
      if (device.current) {
        router.replace("/login/");
        return;
      }
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? `${t("settingsPage.couldNotRevokeThatDevice")} ${err.message}` : t("settingsPage.couldNotRevokeThatDevice"));
    } finally {
      setBusy(false);
    }
  };

  const signOut = async () => {
    try { await auth.logout(); } finally { router.replace("/login/"); }
  };

  return (
    <section className="space-y-4 rounded-lg border border-line p-5" aria-labelledby="devices-heading">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="devices-heading" className="font-semibold">{t("settingsPage.devices")}</h3>
        {session.kind === "browser" && <Button type="button" onClick={signOut}>{t("settingsPage.signOut")}</Button>}
      </div>
      <p className="text-xs text-muted">
        {session.kind === "loopback"
          ? t("settingsPage.thisRequestIsTrustedBecauseItComesFrom")
          : t("settingsPage.signedInAs",{name:session.label ?? t("settingsPage.thisBrowser")}) + " · " + settingsTerm(t,deviceAccess(session.scopes))}
      </p>
      {error && <Banner tone="danger">{error}</Banner>}
      {admin && (
        <>
          {code ? (
            <OpenCode code={code} onDone={() => { setCode(null); void load(); }} />
          ) : (
            <div className="space-y-3">
              <p className="text-sm text-muted">{t("settingsPage.pairAPhoneTabletOrAnotherBrowserWith")}</p>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <label htmlFor="pair-scope" className="block text-sm font-medium">{t("settingsPage.access")}</label>
                  <Select id="pair-scope" value={scope} onChange={(e) => setScope(e.target.value as PairingScope)}>
                    {PAIRING_CHOICES.map((choice) => <option key={choice.value} value={choice.value}>{settingsTerm(t,choice.label)}</option>)}
                  </Select>
                </div>
                <div className="space-y-1">
                  <label htmlFor="pair-label" className="block text-sm font-medium">{t("settingsPage.nameOptional")}</label>
                  <Input id="pair-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder={t("settingsPage.myPhone")} maxLength={80} />
                </div>
              </div>
              <Button type="button" onClick={create} disabled={busy}>{t("settingsPage.createPairingCode")}</Button>
            </div>
          )}
          <div>
            <h4 className="mb-2 text-sm font-medium">{t("settingsPage.signedIn")}</h4>
            {devices === null ? <SkeletonList rows={3} /> : devices.length === 0 ? (
              <EmptyState title={t("settingsPage.noSignedInBrowsersOrPairedDevices")} description={t("settingsPage.createAPairingCodeAboveToAddYour")} className="py-6" />
            ) : (
              <ul className="divide-y divide-line">
                {devices.map((device) => (
                  <li key={device.id} className="flex flex-wrap items-center justify-between gap-2 py-2" data-testid="device-row">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{device.label}{device.current && <span className="ml-2 rounded bg-inset px-1.5 py-0.5 text-xs font-normal">{t("settingsPage.thisBrowser")}</span>}</p>
                      <p className="text-xs text-muted">{device.kind === "browser" ? t("settingsPage.browser") : t("settingsPage.deviceToken")} · {settingsTerm(t,deviceAccess(device.scopes))} {t("settingsPage.lastSeen")} {when(device.last_seen_at)} {t("settingsPage.since")} {when(device.created_at)}</p>
                    </div>
                    <Button type="button" disabled={busy} onClick={() => revoke(device)} aria-label={t("settingsPage.revokeName",{name:device.label})}>
                      {confirming === device.id ? t("settingsPage.confirmRevoke") : t("settingsPage.revoke")}
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </section>
  );
}
