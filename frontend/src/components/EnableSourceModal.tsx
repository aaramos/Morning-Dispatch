import type { ChangeEvent } from "react";
import type { SourceKey, SourceStatus } from "../lib/types";
import { formatSourceLabel } from "../lib/display";

export function EnableSourceModal(props: {
  source: SourceKey;
  status?: SourceStatus;
  webKey: string;
  gmailSecret: string;
  podcastKey: string;
  podcastSecret: string;
  youtubeKey: string;
  fredKey: string;
  busy: boolean;
  onClose: () => void;
  onWebKeyChange: (value: string) => void;
  onGmailSecretChange: (value: string) => void;
  onGmailFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onPodcastKeyChange: (value: string) => void;
  onPodcastSecretChange: (value: string) => void;
  onYoutubeKeyChange: (value: string) => void;
  onFredKeyChange: (value: string) => void;
  onSaveWeb: () => void;
  onSaveGmailSecret: () => void;
  onConnectGmail: () => void;
  onSavePodcast: () => void;
  onSaveYoutube: () => void;
  onSaveFred: () => void;
  onSetupCollections: () => void;
  onRetry: () => void;
}) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <section className="enable-card">
        <button type="button" className="modal-close" onClick={props.onClose} aria-label="Close">×</button>
        <p className="section-kicker">Enable Source</p>
        <h2>Connect {props.status?.label ?? formatSourceLabel(props.source)}</h2>
        <p>{props.status?.reason ?? "This source needs setup before it can be selected."}</p>
        {props.source === "web_search" || props.source === "foreign_media" ? (
          <label>
            Web Search API key
            <input
              type="password"
              value={props.webKey}
              onChange={(event) => props.onWebKeyChange(event.target.value)}
              placeholder="Paste API key"
            />
            <button type="button" onClick={props.onSaveWeb} disabled={props.busy || !props.webKey.trim()}>
              Connect {props.source === "foreign_media" ? "Foreign Media" : "Web Search"}
            </button>
          </label>
        ) : null}
        {props.source === "gmail" ? (
          <div className="enable-stack">
            <button type="button" onClick={props.onConnectGmail} disabled={props.busy}>Connect Gmail</button>
            <label>
              OAuth client JSON file
              <input type="file" accept=".json,application/json" onChange={props.onGmailFileChange} />
            </label>
            <label>
              OAuth client JSON
              <textarea
                value={props.gmailSecret}
                onChange={(event) => props.onGmailSecretChange(event.target.value)}
                rows={5}
                placeholder='{"installed": ... }'
              />
              <button type="button" onClick={props.onSaveGmailSecret} disabled={props.busy || !props.gmailSecret.trim()}>
                Save OAuth Client
              </button>
            </label>
          </div>
        ) : null}
        {props.source === "podcasts" ? (
          <div className="enable-stack">
            <label>
              Podcast Index API key
              <input type="password" value={props.podcastKey} onChange={(event) => props.onPodcastKeyChange(event.target.value)} />
            </label>
            <label>
              Podcast Index API secret
              <input type="password" value={props.podcastSecret} onChange={(event) => props.onPodcastSecretChange(event.target.value)} />
            </label>
            <button type="button" onClick={props.onSavePodcast} disabled={props.busy || !props.podcastKey.trim() || !props.podcastSecret.trim()}>
              Connect Podcasts
            </button>
          </div>
        ) : null}
        {props.source === "youtube" ? (
          <label>
            YouTube Data API key
            <input
              type="password"
              value={props.youtubeKey}
              onChange={(event) => props.onYoutubeKeyChange(event.target.value)}
              placeholder="Paste API key"
            />
            <button type="button" onClick={props.onSaveYoutube} disabled={props.busy || !props.youtubeKey.trim()}>
              Connect YouTube
            </button>
          </label>
        ) : null}
        {props.source === "collections" ? (
          <div className="enable-stack">
            <p>{props.status?.root_path ? `Folder: ${props.status.root_path}` : "Collections uses local folders on this Mac."}</p>
            <button type="button" onClick={props.onSetupCollections} disabled={props.busy}>
              Create Collections Folder
            </button>
          </div>
        ) : null}
        {props.source === "markets" ? (
          <div className="enable-stack">
            <p>Markets uses free public-market data. For rich macroeconomic indicators (yield curve, interest rates, inflation, etc.), you can optionally provide a free FRED API key.</p>
            <label style={{ display: "flex", flexDirection: "column", gap: "6px", width: "100%", boxSizing: "border-box" }}>
              FRED API Key (optional)
              <input
                type="password"
                value={props.fredKey}
                onChange={(event) => props.onFredKeyChange(event.target.value)}
                placeholder="Paste FRED API key"
              />
            </label>
            <button type="button" onClick={props.onSaveFred} disabled={props.busy || !props.fredKey.trim()}>
              Save FRED Key
            </button>
            <div style={{ marginTop: "12px", borderTop: "1px solid var(--line)", paddingTop: "12px", display: "flex", justifyContent: "flex-end" }}>
              <button type="button" onClick={props.onRetry} disabled={props.busy}>Retry Markets</button>
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
