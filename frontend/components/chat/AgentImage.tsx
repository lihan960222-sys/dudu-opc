"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Avatar } from "@/components/ui/Avatar";
import { workspaceScope } from "@/lib/workspace-scope";

// The same portrait can appear in the header, roster, recent chats and transcript.
// Keep its object URL for exactly as long as at least one of those views uses it.
type PictureRequest = { result: Promise<string | null>; readers: number };
const pictures = new Map<string, PictureRequest>();

// An agent's picture: its generated image when it has one, else the letter avatar.
// `tick` changes when the picture is set or removed, so it is fetched again.
export function useAgentImage(agentId: number | null, tick = 0) {
  const key = agentId === null ? null : `${workspaceScope()}:${agentId}:${tick}`;
  const [picture, setPicture] = useState<{ key: string; url: string | null } | null>(null);
  useEffect(() => {
    if (key === null || agentId === null) return;
    let request = pictures.get(key);
    if (!request) {
      request = { result: api.agentAvatarUrl(agentId).catch(() => null), readers: 0 };
      pictures.set(key, request);
    }
    const current = request;
    current.readers += 1;
    let live = true;
    current.result.then(url => { if (live) setPicture({ key, url }); });
    return () => {
      live = false;
      if (--current.readers === 0) {
        pictures.delete(key);
        current.result.then(url => { if (url) URL.revokeObjectURL(url); });
      }
    };
  }, [agentId, key]);
  return picture?.key === key ? picture?.url ?? null : null;
}

type PictureProps = { agentId: number | null; name?: string; size?: "sm" | "md" | "lg" };

export function AgentPortrait({ src, agentId, name, size = "md" }: PictureProps & { src: string | null }) {
  if (!src) return <Avatar agentId={agentId} name={name} size={size} />;
  const px = size === "lg" ? "size-11" : size === "sm" ? "size-6" : "size-8";
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={src} alt="" aria-hidden="true" className={`${px} shrink-0 rounded-full object-cover`} />;
}

export function AgentImage({ agentId, name, size = "md", tick = 0 }: PictureProps & { agentId: number; tick?: number }) {
  const src = useAgentImage(agentId, tick);
  return <AgentPortrait src={src} agentId={agentId} name={name} size={size} />;
}
