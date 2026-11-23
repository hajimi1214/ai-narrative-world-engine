# Changelog

## 0.4.0-rc.1 - 2026-11-23

- Consolidated the previous correctness, recovery, literary evaluation and character-voice changes into a local release candidate.
- Added a complete fake-model authoring acceptance that exports a manuscript after backup restoration.
- Added a Chinese author guide, CI checks, MIT licensing and safer local PostgreSQL defaults.
- Kept literary quality claims at `NOT_EVALUATED` because no real model corpus or reader ratings were collected.

## 0.4.0-rc.1 candidate scope

- Required chapter events and foreshadowing payoffs need critic-confirmed, fingerprint-bound excerpts from the exact prose draft before automatic adoption. Foreshadowing is marked paid off only after adoption.
- Book-level automatic cross-volume continuation remains disabled by default. A passing volume is sealed with a continuity snapshot before the same run advances, and a missing next-volume contract pauses for author action.
- Character voice guidance is optional and author-controlled. It does not constitute evidence of improved literary quality.

## 0.3.0 - 2026-11-21

- Added durable worker leases, restart recovery, transient provider backoff, and persisted writing checkpoints.
- Added author-guided stage recovery for world scenes, Writer drafts, quality review, repair, and adoption without duplicate adoption.
- Added explicit recovery states for waiting retry, budget pause, author action, uncertain provider usage, and API connection loss.

## 0.2.0 - 2026-11-21

- Added continuous author-guided chapter production across rolling volume windows, with chapter and token budgets that resume from saved checkpoints.
- Fixed planned-window continuation so preplanned chapters are reused in order instead of skipped. Starting a new volume now creates a run from the sealed continuity snapshot.
- Added author editing, immutable draft history, version comparison and restore-as-new-draft, quality finding navigation, and manuscript export.
- Added immutable versioned volume snapshots, expanded backup and restore coverage, and tightened quality retry behavior.
- Upgraded legacy SQLite snapshot schemas while preserving the sealed snapshot and allowing later versions.
- Verified that books can start and resume their writing runs independently while repeated requests within each book reuse the same run.
- Improved live status polling, mobile project navigation, responsive authoring and run controls.
