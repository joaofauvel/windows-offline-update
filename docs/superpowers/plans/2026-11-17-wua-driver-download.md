# Windows Update Agent driver download plan

The Catalog route remains the first choice. For an exact driver offer whose
standalone package is unavailable, `wuo update --only drivers --driver-cache`
can request the driver through Windows Update Agent and export its files for
manual servicing. This opt-in route uses the Windows-managed download cache.

- [x] Record the exact agent offer revision and test when the fallback is eligible.
- [x] Recheck the offered revision and present device before requesting the download.
- [x] Export to a temporary directory, verify the resulting driver package, and
  publish the verified files and manifest together.
- [x] Verify dry-run behavior, errors, and repeat runs in tests. Exercise the
  real driver download and the built CLI in the disposable Windows 11 guest.
- [x] On export failure, search by hardware ID and show same-version,
  release-compatible native-architecture Catalog candidates for review.

On Windows 11 25H2 x64, WUA downloaded the exact Voice Clarity revision 1
offer and exported its CAB and loose driver files. The built CLI then
verified the matching signed INF and published the verified copy. The guest
driver-store inventory stayed the same. The lab exercised the audio driver.
