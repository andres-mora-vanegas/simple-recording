#!/bin/sh
# Instala Grabadora para el usuario actual (sin sudo).
# Para desinstalar:  ./instalar.sh --desinstalar
set -e

APP_DIR="$HOME/.local/share/grabadora"
BIN="$HOME/.local/bin/grabadora"
DESKTOP="$HOME/.local/share/applications/org.local.Grabadora.desktop"

if [ "$1" = "--desinstalar" ]; then
    rm -rf "$APP_DIR"
    rm -f "$BIN" "$DESKTOP"
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    echo "Grabadora desinstalada."
    exit 0
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$APP_DIR" "$(dirname "$BIN")" "$(dirname "$DESKTOP")"
install -m 755 "$SRC_DIR/grabadora.py" "$APP_DIR/grabadora.py"

cat > "$BIN" <<EOF
#!/bin/sh
exec python3 "$APP_DIR/grabadora.py" "\$@"
EOF
chmod 755 "$BIN"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Grabadora
GenericName=Grabador de pantalla
Comment=Graba la pantalla o un área, con audio
Exec=$BIN
Icon=media-record
Terminal=false
Categories=AudioVideo;Video;Recorder;
Keywords=grabar;pantalla;screencast;video;recorder;
StartupNotify=true
StartupWMClass=org.local.Grabadora
Actions=alternar;

[Desktop Action alternar]
Name=Empezar o detener grabación
Exec=$BIN --toggle
EOF

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Grabadora instalada. Búscala en el menú como «Grabadora»."
