# Bring-up: from blank CM4 to SSH access

This covers phase 0 of the roadmap for development. End users will flash the ready-made PiDSP image instead, once it exists.

Tested with a CM4 (32 GB eMMC) on a DFRobot PiTray mini, flashed from an Arch Linux host with Raspberry Pi Imager 2.0.12 and Raspberry Pi OS Lite 64-bit (Debian 13 trixie).

## 1. Flash Raspberry Pi OS Lite to the CM4 eMMC

The CM4's eMMC is flashed over USB from another computer.

1. On the carrier board (DFRobot PiTray mini), set the **boot/flash switch to USB (eMMC) mode**. See the DFRobot wiki page for the board for its exact location.
2. Connect the board's USB-C port to your computer. On the PiTray mini this one port carries both power and data.
3. Run **rpiboot** ([raspberrypi/usbboot](https://github.com/raspberrypi/usbboot)) so the eMMC shows up as a USB drive. On Arch, rpiboot is the AUR package `rpiusbboot` and Imager is `rpi-imager` in `extra`.

   ```sh
   sudo rpiboot
   ```

   **If the drive never appears:** with current usbboot releases, the default loader (`mass-storage-gadget64`) boots a small Linux on the CM4. On the PiTray mini, powered from a PC USB port, it got as far as "Second stage boot server done", then the CM4 disconnected and never came back. The older MSD loader works instead. usbboot dropped its CM4 file in 2026-09, so this script fetches it from the last tag that had it and checks its hash:

   ```sh
   deploy/rpiboot-cm4-legacy.sh
   ```

   The eMMC then appears as `RPi-MSD- 0001` after a few seconds. If your desktop auto-mounts its partitions, unmount them (`udisksctl unmount -b /dev/sdXN`) before flashing.

4. Start **Raspberry Pi Imager** and choose *Raspberry Pi 4 / CM4* → *Raspberry Pi OS Lite (64-bit)* → the eMMC drive. On a Wayland-only desktop, Imager re-launches itself as root and looks for X11. This works:

   ```sh
   sudo env WAYLAND_DISPLAY=$WAYLAND_DISPLAY XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR QT_QPA_PLATFORM=wayland rpi-imager
   ```

5. Under OS customisation:
   - Hostname: `PiDSP` (reachable as `PiDSP.local`)
   - Username and password of your choice
   - Locale / timezone / keyboard
   - Services: enable **SSH**, *public-key authentication only*, and paste the dev machine's public key
   - Wi-Fi: leave empty (wired Ethernet is used)

   Current images apply these settings with **cloud-init** (`user-data`, `network-config` and `meta-data` on the boot partition). The old `firstrun.sh` is no longer used.
6. Write the image, set the switch back to normal boot, then power-cycle with Ethernet connected. The first boot takes 1–2 minutes while cloud-init runs.

## 2. First login and passwordless sudo

The Pi gets a DHCP address on first boot:

```sh
ssh <user>@PiDSP.local     # or look up the IP in your router
```

Images customised by Imager through cloud-init leave out the passwordless-sudo rule that older images had. The deploy scripts run `sudo` over a non-interactive SSH session, so add it once. This asks for the password you set in Imager:

```sh
ssh -t <user>@PiDSP.local "echo '<user> ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/010_<user>-nopasswd && sudo chmod 440 /etc/sudoers.d/010_<user>-nopasswd"
ssh <user>@PiDSP.local sudo -n true && echo ok
```

## 3. Static IP

```sh
deploy/set-static-ip.sh <user>@PiDSP.local 192.168.1.50/24 192.168.1.1
```

Pick an address outside your router's DHCP pool, or reserve it in the router. The SSH session drops when the new address is applied. Reconnect on the new IP.

On a fresh image the Ethernet profile (`Wired connection 1`) is created automatically and only exists in memory (`/run/NetworkManager`). The script modifies it, which saves it to `/etc/NetworkManager/system-connections/`, and stops with an error if it was not saved. Reboot once and check that the address stays.

## 4. CamillaDSP and audio plumbing

```sh
deploy/install-camilladsp.sh <user>@PiDSP.local
```

This installs a pinned, checksum-verified CamillaDSP to `/usr/local/bin` and a `camilladsp` system user with `/var/lib/camilladsp`. It loads the ALSA loopback (`snd-aloop`, card `Loopback`) at boot, which carries measurement sweeps through CamillaDSP. It sets the CM6206 test card to its S/PDIF input with the output at 0 dB, and saves that. It also enables `camilladsp.service`, with the websocket on `127.0.0.1:1234` only, waiting for a config. The script is safe to re-run.

## 5. Next

The PiDSP backend and web UI will be installed by another script, once they exist.
