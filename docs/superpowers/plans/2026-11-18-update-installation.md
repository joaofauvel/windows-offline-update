# Update installation and ordering for 0.3.1

`wuo update` should install offered updates through their supported Windows or recovery route and state what remains incomplete.

- [ ] Install Windows security components through exact Windows Update Agent identities and revisions. Order Defender platform, Windows Security app/service, then intelligence with its included engine. Persist progress and stop at restart barriers.
- [ ] Verify native installation results and installed state. Resume verification after restart and keep recovery jobs from running across an unfinished Windows installation.
- [ ] Generalize offline MSU/CAB servicing through CBS package metadata and applicability. Retain dependency order and hooks, and verify installed package identities after boot.
- [ ] Route full feature upgrades through Windows Setup compatibility and boot-phase requirements. Report any route lacking a supported way to preserve configured boot-critical hooks as incomplete.
- [ ] Validate native security installation in the disposable VM, regression-test ordering and resume behavior, document support limits, and release version 0.3.1 after CI.
