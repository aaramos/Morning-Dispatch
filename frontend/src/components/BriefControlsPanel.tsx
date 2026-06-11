import type { BriefControlsDraft, SourceKey } from "../lib/types";
import { defaultBriefControls } from "../lib/types";
import { NumberStepper } from "./NumberStepper";
import { RecencyControl } from "./RecencyControl";
import { ContentLimitsPanel } from "./ContentLimitsPanel";

export function BriefControlsPanel(props: {
  controls: BriefControlsDraft;
  defaults: BriefControlsDraft;
  sourceSelection: Record<SourceKey, boolean>;
  showReset?: boolean;
  onChange: (controls: BriefControlsDraft) => void;
}) {
  const presets = props.controls.youtube_presets ?? defaultBriefControls.youtube_presets!;
  const podcastPresets = props.controls.podcast_presets ?? defaultBriefControls.podcast_presets!;
  const gmailPresets = props.controls.gmail_presets ?? defaultBriefControls.gmail_presets!;

  return (
    <div className="brief-controls-panel">
      <RecencyControl
        label="Default recency"
        value={props.controls.lookback_hours}
        onChange={(lookback_hours) => props.onChange({ ...props.controls, lookback_hours })}
      />
      <ContentLimitsPanel
        limits={props.controls.content_limits}
        defaults={props.defaults.content_limits}
        sourceSelection={props.sourceSelection}
        showReset={false}
        onChange={(content_limits) => props.onChange({ ...props.controls, content_limits })}
        youtubePresets={props.controls.youtube_presets}
        podcastPresets={props.controls.podcast_presets}
        gmailPresets={props.controls.gmail_presets}
      />
      <div className="settings-youtube-presets" style={{ marginTop: "24px", paddingTop: "18px", borderTop: "1px solid var(--line)" }}>
        <strong>YouTube scale presets</strong>
        <p className="muted" style={{ margin: "4px 0 12px", fontSize: "0.85rem" }}>Configure per-source video limits for YouTube for each profile scale (Max 40).</p>
        <div className="content-limit-grid">
          <NumberStepper
            label="Max profile"
            value={presets.max}
            min={1}
            max={40}
            onChange={(val) => props.onChange({
              ...props.controls,
              youtube_presets: { ...presets, max: val }
            })}
          />
          <NumberStepper
            label="Large profile"
            value={presets.large}
            min={1}
            max={40}
            onChange={(val) => props.onChange({
              ...props.controls,
              youtube_presets: { ...presets, large: val }
            })}
          />
          <NumberStepper
            label="Medium profile"
            value={presets.medium}
            min={1}
            max={40}
            onChange={(val) => props.onChange({
              ...props.controls,
              youtube_presets: { ...presets, medium: val }
            })}
          />
          <NumberStepper
            label="Focused profile"
            value={presets.focused}
            min={1}
            max={40}
            onChange={(val) => props.onChange({
              ...props.controls,
              youtube_presets: { ...presets, focused: val }
            })}
          />
        </div>
      </div>
      <div className="settings-youtube-presets" style={{ marginTop: "24px", paddingTop: "18px", borderTop: "1px solid var(--line)" }}>
        <strong>Podcast scale presets</strong>
        <p className="muted" style={{ margin: "4px 0 12px", fontSize: "0.85rem" }}>Configure per-source limits for podcast items for each profile scale (Max 40).</p>
        <div className="content-limit-grid">
          <NumberStepper
            label="Max profile"
            value={podcastPresets.max}
            min={1}
            max={40}
            onChange={(val) => props.onChange({
              ...props.controls,
              podcast_presets: { ...podcastPresets, max: val }
            })}
          />
          <NumberStepper
            label="Large profile"
            value={podcastPresets.large}
            min={1}
            max={40}
            onChange={(val) => props.onChange({
              ...props.controls,
              podcast_presets: { ...podcastPresets, large: val }
            })}
          />
          <NumberStepper
            label="Medium profile"
            value={podcastPresets.medium}
            min={1}
            max={40}
            onChange={(val) => props.onChange({
              ...props.controls,
              podcast_presets: { ...podcastPresets, medium: val }
            })}
          />
          <NumberStepper
            label="Focused profile"
            value={podcastPresets.focused}
            min={1}
            max={40}
            onChange={(val) => props.onChange({
              ...props.controls,
              podcast_presets: { ...podcastPresets, focused: val }
            })}
          />
        </div>
      </div>
      <div className="settings-youtube-presets" style={{ marginTop: "24px", paddingTop: "18px", borderTop: "1px solid var(--line)" }}>
        <strong>Gmail scale presets</strong>
        <p className="muted" style={{ margin: "4px 0 12px", fontSize: "0.85rem" }}>Configure per-source limits for Gmail items for each profile scale (Max 40).</p>
        <div className="content-limit-grid">
          <NumberStepper
            label="Max profile"
            value={gmailPresets.max}
            min={1}
            max={80}
            onChange={(val) => props.onChange({
              ...props.controls,
              gmail_presets: { ...gmailPresets, max: val }
            })}
          />
          <NumberStepper
            label="Large profile"
            value={gmailPresets.large}
            min={1}
            max={80}
            onChange={(val) => props.onChange({
              ...props.controls,
              gmail_presets: { ...gmailPresets, large: val }
            })}
          />
          <NumberStepper
            label="Medium profile"
            value={gmailPresets.medium}
            min={1}
            max={80}
            onChange={(val) => props.onChange({
              ...props.controls,
              gmail_presets: { ...gmailPresets, medium: val }
            })}
          />
          <NumberStepper
            label="Focused profile"
            value={gmailPresets.focused}
            min={1}
            max={80}
            onChange={(val) => props.onChange({
              ...props.controls,
              gmail_presets: { ...gmailPresets, focused: val }
            })}
          />
        </div>
      </div>
      {props.showReset !== false ? (
        <button type="button" className="ghost-action reset-limits-action" onClick={() => props.onChange(props.defaults)}>
          Reset to defaults
        </button>
      ) : null}
    </div>
  );
}
