# Bring-up: from blank CM4 to SSH access

This covers phase 0 of the roadmap for development. End users will flash the ready-made PiDSP image instead, once it exists.

## 1. Flash Raspberry Pi OS Lite to the CM4 eMMC

The CM4's eMMC is flashed over USB from another computer:

1. On the carrier board (DFRobot PiTray mini), set the **boot/flash switch to USB (eMMC) mode**. See the DFRobot wiki page for the board for its exact location.
2. Connect the board's USB-C port to your computer.
3. Run **rpiboot** ([raspberrypi/usbboot](https://github.com/raspberrypi/usbboot)). Recent versions of Raspberry Pi Imager can also do this step. The eMMC then shows up as a USB drive.
4. In **Raspberry Pi Imager**, choose *Raspberry Pi 4 / CM4* → *Raspberry Pi OS Lite (64-bit)* → the eMMC drive.
5. Under OS customisation:
   - Hostname: `PiDSP` (reachable as `PiDSP.local`)
   - Username and password of your choice
   - Locale / timezone
   - Services: enable **SSH**, *public-key authentication only*, and paste the dev machine's public key
   - Wi-Fi: leave empty (wired Ethernet is used)
6. Write the image, set the switch back to normal boot, then power-cycle with Ethernet connected.

## 2. First login

The Pi gets a DHCP address on first boot:

```sh
ssh <user>@PiDSP.local     # or look up the IP in your router
```

## 3. Static IP

```sh
deploy/set-static-ip.sh <user>@PiDSP.local 192.168.1.50/24 192.168.1.1
```

Pick an address outside your router's DHCP pool, or reserve it in the router. The SSH session drops when the new address is applied. Reconnect on the new IP.

## 4. Next

Installing CamillaDSP and checking the sound card and microphone come next. A script for this will be added in `deploy/`.
