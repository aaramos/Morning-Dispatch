# Morning Dispatch Backlog

## Later: Digest Onboarding and Preference Setup

**Status:** Backlog  
**Priority:** After the single-digest pipeline is stable  
**Goal:** Let a user create and tune one or more digests without manual database/source seeding.

### User Problem

Morning Dispatch currently works from manually configured digest interests, Gmail senders, Reddit sources, and feedback signals. There is no guided setup flow that asks what the user wants to track, which sources to trust, or what should be ignored.

### Desired Outcome

A non-technical user can open the app, create a digest, describe their interests in plain English, connect/select sources, review the proposed configuration, and let feedback refine future ranking.

### Scope

- Guided digest creation flow for interest text, preferred topics, and excluded topics.
- Source setup for Gmail newsletters and Reddit communities.
- Review screen showing what the app thinks the digest should track.
- Support for multiple independent digests with their own interests, sources, thresholds, and feedback.
- Preference profile updates from `Useful` / `Not useful` feedback.

### Not in This Item

- Multi-user cloud accounts.
- Billing, sharing, or public deployment.
- Replacing the current local-first pipeline.

### Acceptance Criteria

- A new digest can be created without editing seed data or SQLite directly.
- The user can see and edit interests and sources before the first run.
- Each digest keeps separate source lists, feedback, and ranking behavior.
- Existing single-digest behavior keeps working.
