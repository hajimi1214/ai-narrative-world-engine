# Changelog

## 0.2.0 - 2026-11-21

- Added continuous author-guided chapter production across rolling volume windows, with chapter and token budgets that resume from saved checkpoints.
- Fixed planned-window continuation so preplanned chapters are reused in order instead of skipped. Starting a new volume now creates a run from the sealed continuity snapshot.
- Added author editing, immutable draft history, version comparison and restore-as-new-draft, quality finding navigation, and manuscript export.
- Added immutable versioned volume snapshots, expanded backup and restore coverage, and tightened quality retry behavior.
- Upgraded legacy SQLite snapshot schemas while preserving the sealed snapshot and allowing later versions.
- Verified that books can start and resume their writing runs independently while repeated requests within each book reuse the same run.
- Improved live status polling, mobile project navigation, responsive authoring and run controls.
