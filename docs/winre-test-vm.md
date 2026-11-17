# Disposable Windows 11 VM for WinRE testing

Use a fresh Windows guest on file-backed virtual disks. The laptop has 31 GiB of RAM, roughly 118 GiB of free space, and an active Windows hypervisor. Windows 11 Home supports Windows Hypervisor Platform but lacks the full Hyper-V VM management role. Oracle documents that VirtualBox can use Windows Hypervisor Platform when the Windows hypervisor is active. The compatibility path may run more slowly. [Microsoft's Hyper-V edition requirements](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/install-hyper-v), [Oracle VirtualBox documentation](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/EN-VBOX-7-2-USER.pdf)

## Prepare the host

1. Download the English Windows 11 Enterprise 25H2 **x64** evaluation ISO. Choose **ISO – Enterprise download**. It is a 90-day evaluation intended for a disposable guest. [Microsoft Evaluation Center](https://www.microsoft.com/en-us/evalcenter/evaluate-windows-11-enterprise), [ISO download choices](https://www.microsoft.com/en-us/evalcenter/download-windows-11-enterprise)
2. On this laptop, Windows Hypervisor Platform is currently disabled. If you choose to run the VM locally, open **Turn Windows features on or off**, enable **Windows Hypervisor Platform**, and schedule the host restart when you're ready. This is the one host feature change for the proposed setup. Given your external Windows boot setup, check Windows Update for a pending restart and keep your recovery media handy before taking that step. [Oracle VirtualBox and the Windows hypervisor](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/EN-VBOX-7-2-USER.pdf)
3. Install Oracle VirtualBox for Windows hosts. Use the base package for this lab. It provides the virtual disk, EFI, TPM, and snapshot features used below. [Oracle's installer documentation](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/installation.html)

## Create and install the guest

1. In VirtualBox Manager, create **WinRE-25H2-Lab**, select the ISO, and choose **Windows 11 (64-bit)**. Skip unattended installation so the installer and selected disk are visible.
2. Assign **4 vCPUs**, **8 GiB RAM**, and a **dynamically allocated 80 GiB VDI**. In the VM's firmware settings, enable **EFI**, **Secure Boot**, and **TPM 2.0**. These are guest settings. [VirtualBox VM settings](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/working-with-vms.html), [Windows 11 VM requirements](https://learn.microsoft.com/en-us/windows/whats-new/windows-11-requirements#virtual-machine-support)
3. In **Storage**, verify that the guest has only the lab VDI and the installation ISO. Leave USB filters and shared folders empty. Use **NAT** during setup if connectivity is required, then disconnect the guest's virtual network cable before recording the offline-update baseline.
4. Start the VM and point Windows Setup at the new **80 GiB unallocated virtual disk**. After installation, eject the ISO from the VM's virtual optical drive and run `winver` inside the guest. Record the edition, release, and full build number.

## Prove the recovery baseline

1. Shut down the guest. In VirtualBox Manager, select the VM, open **Snapshots**, and take a powered-off snapshot named `clean-25h2`.
2. Start the guest. Confirm **Settings → System → Recovery → Advanced startup → Restart now** reaches **Troubleshoot → Advanced options → Command Prompt**. Run these read-only commands at the **guest's** WinRE prompt. Identify which guest volume contains `Windows\System32\Config\SYSTEM`.

   ```cmd
   diskpart
   list disk
   list volume
   exit
   ```

3. Shut down the guest and restore `clean-25h2`. Confirm it boots and reports the original build. Save lab logs outside the VM before restoring a later snapshot.
4. When the CLI is ready, choose a Microsoft cumulative update newer than the guest's baseline and test the generated runner in this guest. Clone the baseline for a second case with a separate EFI VDI. Verify both VDI files are connected in normal, snapshottable mode before testing that split layout. Physical USB4 behavior remains outside the VM's coverage. [VirtualBox snapshot and clone behavior](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/working-with-vms.html#snapshots)
