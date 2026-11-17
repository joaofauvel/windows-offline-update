# KB5132061 recovery validation

The test guest ran Windows 11 Enterprise Evaluation 25H2 x64 from file-backed Windows and EFI disks. Both disks were snapshotted as `pre-update` and restored before servicing. The guest had no network adapter. QEMU ran inside a dedicated WSL distro. Secure Boot was disabled in the guest.

## Observed results

| Check | Result |
| --- | --- |
| Baseline | Windows 11 25H2 build `26200.6584` |
| Package preparation | Microsoft Catalog digests matched both MSUs. The extracted `SSU-26100.9971-x64.cab` matched the expected digest. |
| Wrong EFI selection | The runner rejected the optical drive supplied as EFI. |
| Correct target | In the Command Prompt from Microsoft installation media, the runner verified `C:` as Windows and `S:` as EFI and returned success. |
| Offline servicing | DISM returned `0` for the servicing stack and the target cumulative package. The runner recorded a successful staged result. |
| Registry hooks | The post-servicing hook changed a lab DWORD from `3` to `0`. After a deliberate reset to `3`, `repair-hooks` restored `0`. The recovery log still had exactly two DISM results. |
| First boot | The guest booted to the Windows lock screen. A read-only check after returning to recovery verified build `26200.9974`, the cumulative package in the `Installed` state, and the lab DWORD still at `0`. |

The recovery image lacked `findstr.exe`, so the runner parses the relevant output with CMD built-ins. CMD `copy` skipped a hidden SOFTWARE hive transaction log, so the probe uses `xcopy /h`. Windows CI includes a hidden transaction-log fixture.

This run used the Microsoft installation media Command Prompt. On-disk WinRE, Secure Boot enabled inside the guest, and physical USB4 boot behavior remain unverified.
