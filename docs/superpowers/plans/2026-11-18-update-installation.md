# Update installation and ordering for 0.3.1

`wuo update` should install offered updates through their supported Windows or recovery route and state what remains incomplete.

- [x] Install Windows security components through exact Windows Update Agent identities and revisions. Order Defender platform, Windows Security app/service, then intelligence with its included engine. Persist progress and stop at restart barriers.
- [x] Verify native installation results and installed state. Resume verification after restart and keep recovery jobs from running across an unfinished Windows installation.
- [x] Generalize offline MSU/CAB servicing through CBS package metadata and applicability. Retain dependency order and hooks, and verify the selected target's main package identities. Audit all payload identities separately because already-satisfied checkpoint dependencies can be skipped by DISM.
- [x] Report full Windows Setup feature upgrades as incomplete until hooks can be applied to the replacement SYSTEM hive before its first boot. Record the supported Setup boundaries and required VM validation in `docs/feature-upgrades.md`.
- [ ] Validate native security installation in the disposable VM, regression-test ordering and resume behavior, document support limits, and release version 0.3.1 after CI.

Full Setup orchestration remains deferred. The 0.3.1 release gate is a fresh generic cumulative apply and post-boot verification, followed by passing CI and publication. Native security installation and hook persistence have already been verified in a disposable VM.
