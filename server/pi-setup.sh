#!/bin/bash
# Turns a Raspberry Pi into a SyncBox server with a website.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# On the Pi:
#   git clone <this repo> && cd SyncBox
#   bash server/pi-setup.sh                 # asks where to save the files (SD card or a USB drive)
#   bash server/pi-setup.sh /mnt/ssd/sync   # or say it straight away
#   bash server/pi-setup.sh --move          # later: move your files to another place
#   git pull && bash server/pi-setup.sh --update   # install a new version (keeps files and password)
set -e

APP_DIR=/opt/syncbox
SERVICE=/etc/systemd/system/syncbox.service
DEFAULT_DIR=/srv/syncbox
FSTAB=/etc/fstab
MNT_BASE=/mnt
ME="$(whoami)"
HERE="$(cd "$(dirname "$0")" && pwd)"

# ------------------------------------------------------------ helpers

human() { numfmt --to=iec --suffix=B "$1" 2>/dev/null || echo "$1 bytes"; }

# free space (in bytes) of the nearest existing folder above $1
free_bytes() {
  local d="$1"
  while [ ! -d "$d" ]; do d="$(dirname "$d")"; done
  df -P -B1 "$d" | awk 'NR==2 {print $4}'
}

ask_yes() {  # ask_yes "question" -> true when the answer starts with y
  local a
  read -r -p "$1 [y/N] " a
  case "$a" in [yY]*) return 0 ;; *) return 1 ;; esac
}

# The disk the system runs from (its partitions are never offered).
root_disk() {
  local src
  src="$(findmnt -no SOURCE / 2>/dev/null)"
  lsblk -no PKNAME "$src" 2>/dev/null | head -1
}

# Fills DRIVES with lines "device|fstype|size|label|uuid|mountpoint" for every
# drive partition SyncBox can use (USB sticks, USB SSDs, hard disks).
list_drives() {
  DRIVES=()
  local rd line NAME FSTYPE SIZE LABEL UUID MOUNTPOINT TYPE PKNAME
  rd="$(root_disk)"
  while IFS= read -r line; do
    NAME="" FSTYPE="" SIZE="" LABEL="" UUID="" MOUNTPOINT="" TYPE="" PKNAME=""
    eval "$line"
    [ "$TYPE" = part ] || [ "$TYPE" = disk ] || continue
    [ -n "$FSTYPE" ] && [ -n "$UUID" ] || continue
    case "$FSTYPE" in swap|LVM2_member|crypto_LUKS|linux_raid_member|squashfs|iso9660) continue ;; esac
    [ -n "$rd" ] && { [ "$PKNAME" = "$rd" ] || [ "${NAME##*/}" = "$rd" ]; } && continue
    case "$MOUNTPOINT" in /|/boot|/boot/*|\[SWAP\]) continue ;; esac
    DRIVES+=("$NAME|$FSTYPE|$SIZE|$LABEL|$UUID|$MOUNTPOINT")
  done < <(lsblk -P -p -o NAME,FSTYPE,SIZE,LABEL,UUID,MOUNTPOINT,TYPE,PKNAME 2>/dev/null)
}

# Makes sure a drive is mounted at a fixed place on every boot (/etc/fstab),
# so the files are always in the same folder. Prints that folder.
mount_drive() {
  local dev="$1" fstype="$2" label="$3" uuid="$4" mnt="$5" target type opts name
  local existing
  existing="$(awk -v u="UUID=$uuid" '$1==u {print $2}' "$FSTAB")"
  if [ -n "$existing" ]; then
    sudo mkdir -p "$existing"
    mountpoint -q "$existing" || sudo mount "$existing" >&2
    echo "$existing"
    return
  fi
  name="$(echo "$label" | tr -cd 'A-Za-z0-9_-' | cut -c1-30)"
  target="$MNT_BASE/${name:-syncbox-drive}"
  [ -e "$target" ] && ! mountpoint -q "$target" && [ -n "$(ls -A "$target" 2>/dev/null)" ] && target="$MNT_BASE/syncbox-$uuid"
  type="$fstype"
  case "$fstype" in
    vfat|exfat) opts="defaults,nofail,uid=$(id -u),gid=$(id -g),umask=022" ;;
    ntfs)
      echo "   (installing NTFS support)" >&2
      sudo apt-get install -y ntfs-3g >/dev/null
      type=ntfs-3g; opts="defaults,nofail,uid=$(id -u),gid=$(id -g),umask=022" ;;
    *) opts="defaults,nofail" ;;
  esac
  [ -n "$mnt" ] && sudo umount "$mnt" >&2 || true
  sudo mkdir -p "$target"
  echo "UUID=$uuid  $target  $type  $opts  0  2" | sudo tee -a "$FSTAB" >/dev/null
  sudo systemctl daemon-reload >&2 || true
  sudo mount "$target" >&2
  echo "$target"
}

# Asks where the files should go. Sets DATA_DIR.
choose_location() {
  local i n d dev fstype size label uuid mnt choice where free
  list_drives
  echo
  echo "== Where should SyncBox save the files? =="
  echo
  echo "  1) On the SD card ($DEFAULT_DIR), $(human "$(free_bytes "$DEFAULT_DIR")") free"
  n=1
  for d in "${DRIVES[@]}"; do
    n=$((n + 1))
    IFS='|' read -r dev fstype size label uuid mnt <<<"$d"
    where="${label:+\"$label\" }"
    if [ -n "$mnt" ]; then free=", $(human "$(free_bytes "$mnt")") free"; else free=""; fi
    echo "  $n) USB drive ${where}($dev, $size $fstype$free)"
  done
  n=$((n + 1))
  echo "  $n) Type a folder yourself"
  [ ${#DRIVES[@]} -eq 0 ] && echo && echo "  (No USB drive found. Plug one in and run this again to use it.)"
  echo
  read -r -p "Choose 1-$n [1]: " choice
  choice="${choice:-1}"
  if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "$n" ]; then
    echo "That's not one of the choices."; exit 1
  fi

  if [ "$choice" -eq 1 ]; then
    DATA_DIR="$DEFAULT_DIR"
  elif [ "$choice" -eq "$n" ]; then
    read -r -p "Folder (for example /mnt/ssd/syncbox): " DATA_DIR
    DATA_DIR="${DATA_DIR%/}"
    case "$DATA_DIR" in /?*) ;; *) echo "Please type a full path that starts with /."; exit 1 ;; esac
    case "$DATA_DIR" in /media/*)
      echo
      echo "Note: drives under /media are only there while someone is logged in on the Pi's desktop."
      echo "      Choose the drive from the list instead, so it's always there after a restart."
      ask_yes "Use $DATA_DIR anyway?" || exit 1 ;;
    esac
  else
    IFS='|' read -r dev fstype size label uuid mnt <<<"${DRIVES[$((choice - 2))]}"
    if [ "$fstype" = vfat ]; then
      echo
      echo "Note: this drive is FAT32, which can't store files bigger than 4 GB."
      ask_yes "Use it anyway?" || exit 1
    fi
    echo "== Setting up the drive so it's always mounted after a restart =="
    DATA_DIR="$(mount_drive "$dev" "$fstype" "$label" "$uuid" "$mnt")/SyncBox"
  fi
  echo
  echo "Files will be saved in: $DATA_DIR ($(human "$(free_bytes "$DATA_DIR")") free)"
}

write_service() {
  sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=SyncBox server + website
After=network-online.target local-fs.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
User=$ME
ExecStart=/usr/bin/python3 $APP_DIR/syncbox_web.py --root "$DATA_DIR" --port 80
Restart=always
RestartSec=10
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
}

current_dir() {
  sed -n 's/^ExecStart=.* --root \(.*\) --port .*/\1/p' "$SERVICE" | tr -d '"'
}

# ------------------------------------------------------------ move

move_files() {
  local old used
  [ -f "$SERVICE" ] || { echo "SyncBox isn't installed yet. Run: bash server/pi-setup.sh"; exit 1; }
  old="$(current_dir)"
  echo "Your files are now in: $old"
  choose_location
  [ "$(realpath -m "$DATA_DIR")" = "$(realpath -m "$old")" ] && { echo "That's where they already are."; exit 0; }

  if [ -f "$DATA_DIR/.syncbox_hub" ]; then
    echo "There's already a SyncBox folder there."
    ask_yes "Switch to it without copying your current files?" || exit 1
  else
    if [ -d "$DATA_DIR" ] && [ -n "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
      echo "$DATA_DIR isn't empty. Pick an empty folder (or a new one)."; exit 1
    fi
    used="$(du -sb "$old" 2>/dev/null | cut -f1)"
    if [ "${used:-0}" -ge "$(free_bytes "$DATA_DIR")" ]; then
      echo "Not enough space there: your files need $(human "$used")."; exit 1
    fi
    ask_yes "Stop SyncBox and copy $(human "${used:-0}") of files to $DATA_DIR?" || exit 1
    sudo systemctl stop syncbox
    sudo mkdir -p "$DATA_DIR"
    sudo chown "$ME:$ME" "$DATA_DIR"
    echo "== Copying (this can take a while) =="
    if ! cp -a "$old/." "$DATA_DIR/" || \
       [ "$(find "$old" | wc -l)" -ne "$(find "$DATA_DIR" | wc -l)" ]; then
      echo "Copying failed. Nothing was changed: SyncBox keeps using $old."
      sudo systemctl start syncbox
      exit 1
    fi
  fi
  write_service
  sudo systemctl restart syncbox
  echo
  echo "Done! SyncBox now saves your files in: $DATA_DIR"
  echo "Your computers don't need any changes."
  echo "The old copy is still in $old. When everything works, you can delete it with:"
  echo "    sudo rm -rf \"$old\""
}

# ------------------------------------------------------------ install / update

find_client() {
  CLIENT="$HERE/syncbox.py"
  [ -f "$CLIENT" ] || CLIENT="$HERE/../client/syncbox.py"
  if [ ! -f "$HERE/syncbox_web.py" ] || [ ! -f "$CLIENT" ]; then
    echo "Missing files. Run this from the SyncBox repository (server/ and client/ folders)."
    exit 1
  fi
}

update() {
  [ -f "$SERVICE" ] || { echo "SyncBox isn't installed yet. Run: bash server/pi-setup.sh"; exit 1; }
  find_client
  DATA_DIR="$(current_dir)"
  echo "== Updating SyncBox (your files and password stay as they are) =="
  sudo cp "$HERE/syncbox_web.py" "$CLIENT" "$APP_DIR/"
  write_service
  sudo systemctl restart syncbox
  echo "Done. On each Windows computer, download SyncBox again from the website and run it"
  echo "to get the newest app."
}

install() {
  local client
  find_client
  client="$CLIENT"

  echo "== Installing what's needed =="
  sudo apt-get update -y
  sudo apt-get install -y python3 avahi-daemon

  if [ -n "$1" ]; then DATA_DIR="${1%/}"; else choose_location; fi

  echo "== Copying SyncBox to $APP_DIR, files go to $DATA_DIR =="
  sudo mkdir -p "$APP_DIR" "$DATA_DIR"
  sudo cp "$HERE/syncbox_web.py" "$client" "$APP_DIR/"
  sudo chown -R "$ME:$ME" "$DATA_DIR"

  echo
  echo "== Choose the password for the SyncBox website =="
  python3 "$APP_DIR/syncbox_web.py" --set-password

  echo "== Starting SyncBox (and on every boot) =="
  write_service
  sudo systemctl enable --now syncbox avahi-daemon
  sleep 2
  sudo systemctl --no-pager --lines=3 status syncbox || true

  echo
  echo "================================================================"
  echo " Done! Open this on any computer in your home network:"
  echo
  echo "     http://$(hostname).local"
  echo
  echo " Log in, click 'Download SyncBox for Windows', run it. That's it."
  echo " Files are saved in $DATA_DIR."
  echo " To move them later:  bash server/pi-setup.sh --move"
  echo "================================================================"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "$1" in
    --move) move_files ;;
    --update) update ;;
    -h|--help) sed -n '2,10p' "$0" ;;
    *) install "$1" ;;
  esac
fi
