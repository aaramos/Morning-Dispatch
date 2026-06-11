import { useState } from "react";
import { fetchPodcastShows, savePodcastShows } from "../lib/api";
import type { PodcastShowCandidate } from "../lib/api";

export function PodcastShowPicker(props: { ensureTopicId: () => Promise<string | null> }) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [savedNote, setSavedNote] = useState("");
  const [candidates, setCandidates] = useState<PodcastShowCandidate[]>([]);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [stalenessDays, setStalenessDays] = useState(60);

  async function loadShows() {
    setLoading(true);
    setError("");
    setSavedNote("");
    try {
      const topicId = await props.ensureTopicId();
      if (!topicId) {
        setError("Save or build this topic once, then choose shows.");
        return;
      }
      const data = await fetchPodcastShows(topicId);
      setStalenessDays(data.staleness_days);
      setCandidates(data.candidates);
      const initial: Record<string, boolean> = {};
      for (const candidate of data.candidates) {
        initial[candidate.feed_url] = Boolean(candidate.subscribed);
      }
      setSelected(initial);
      setOpen(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load podcast shows");
    } finally {
      setLoading(false);
    }
  }

  async function saveShows() {
    setSaving(true);
    setError("");
    setSavedNote("");
    try {
      const topicId = await props.ensureTopicId();
      if (!topicId) {
        setError("Save or build this topic once, then choose shows.");
        return;
      }
      const shows = candidates
        .filter((candidate) => selected[candidate.feed_url])
        .map((candidate) => ({ feed_url: candidate.feed_url, title: candidate.title }));
      await savePodcastShows(topicId, shows);
      setSavedNote(`Saved ${shows.length} show${shows.length === 1 ? "" : "s"}. The brief will summarize each show's latest episode.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save podcast shows");
    } finally {
      setSaving(false);
    }
  }

  const selectedCount = candidates.filter((candidate) => selected[candidate.feed_url]).length;

  return (
    <div className="podcast-show-picker">
      <div className="podcast-show-picker-head">
        <strong>Podcast shows</strong>
        <button type="button" onClick={() => void loadShows()} disabled={loading || saving}>
          {loading ? "Finding shows…" : open ? "Refresh shows" : "Find & choose shows"}
        </button>
      </div>
      {error ? <p className="meta error">{error}</p> : null}
      {open ? (
        <div className="podcast-show-body">
          <p className="meta">
            Pick the shows to follow. Each build summarizes the latest episode of every show you keep
            (regardless of topic match); shows with no episode in the last {stalenessDays} days are skipped.
          </p>
          <div className="podcast-show-list">
            {candidates.length === 0 ? (
              <p className="meta">No candidate shows found yet. Try broadening the interest.</p>
            ) : (
              candidates.map((candidate) => (
                <label className="podcast-show-row" key={candidate.feed_url}>
                  <input
                    type="checkbox"
                    checked={Boolean(selected[candidate.feed_url])}
                    onChange={(event) =>
                      setSelected((prev) => ({ ...prev, [candidate.feed_url]: event.target.checked }))
                    }
                  />
                  <span className="podcast-show-copy">
                    <span className="podcast-show-title">
                      {candidate.title}
                      {candidate.stale ? <span className="podcast-show-stale"> · stale</span> : null}
                    </span>
                    {candidate.description ? (
                      <span className="podcast-show-desc">{candidate.description}</span>
                    ) : null}
                    {candidate.latest_episode_title ? (
                      <span className="podcast-show-latest">Latest: {candidate.latest_episode_title}</span>
                    ) : null}
                  </span>
                </label>
              ))
            )}
          </div>
          <div className="podcast-show-actions">
            <button type="button" className="secondary-action" onClick={() => void saveShows()} disabled={saving}>
              {saving ? "Saving…" : `Save ${selectedCount} show${selectedCount === 1 ? "" : "s"}`}
            </button>
            {savedNote ? <span className="meta success">{savedNote}</span> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
