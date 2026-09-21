#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' "Run this installer as root." >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DRIVER_REPOSITORY=https://github.com/sn4f/asterisk-chan-quectel.git
DRIVER_COMMIT=d6e9dba8319e9f78eed99b56e8fc4440caf9c4be
ASTERISK_SOURCE_VERSION=22.10.1
ASTERISK_SOURCE_SHA256=0953564c44fa49827f3c9d70ca6e80db83828c9848440852c6be44c961855353
ASTERISK_SOURCE_BASE_URL=https://downloads.asterisk.org/pub/telephony/asterisk/old-releases
VOICE_BUILD_DIR=$(mktemp -d /tmp/ec25toolbox-voice.XXXXXX)

cleanup() {
    case "$VOICE_BUILD_DIR" in
        /tmp/ec25toolbox-voice.*) rm -rf -- "$VOICE_BUILD_DIR" ;;
    esac
}
trap cleanup EXIT HUP INT TERM

build_asterisk_from_source() {
    archive="$VOICE_BUILD_DIR/asterisk-$ASTERISK_SOURCE_VERSION.tar.gz"
    curl -fsSL \
        "$ASTERISK_SOURCE_BASE_URL/asterisk-$ASTERISK_SOURCE_VERSION.tar.gz" \
        -o "$archive"
    printf '%s  %s\n' "$ASTERISK_SOURCE_SHA256" "$archive" | sha256sum -c -
    tar -xzf "$archive" -C "$VOICE_BUILD_DIR"
    cd "$VOICE_BUILD_DIR/asterisk-$ASTERISK_SOURCE_VERSION"
    ./configure \
        --with-pjproject-bundled \
        --with-jansson-bundled \
        --without-dahdi \
        --without-pri \
        --without-unixodbc
    detected_jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '2')
    jobs=${EC25_BUILD_JOBS:-$detected_jobs}
    case "$jobs" in
        ''|*[!0-9]*|0) jobs=2 ;;
    esac
    if ! make -j "$jobs"; then
        printf '%s\n' \
            "Parallel Asterisk build failed; retrying serially with full compiler output." >&2
        # A serial retry avoids Raspberry Pi memory pressure. NOISY_BUILD is
        # Asterisk's supported diagnostic mode and exposes the actual compiler
        # command if the failure is unrelated to memory.
        make -j 1 NOISY_BUILD=yes
    fi
    make install
    make install-headers
}

if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        autoconf automake bison build-essential curl flex git libasound2-dev \
        libcurl4-openssl-dev libedit-dev libjansson-dev libncurses-dev \
        libnewt-dev libsqlite3-dev libssl-dev libtool libxml2-dev pkg-config \
        libsrtp2-dev uuid-dev
    if apt-cache show asterisk >/dev/null 2>&1 && apt-cache show asterisk-dev >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y asterisk asterisk-dev
    else
        printf '%s\n' "Distribution Asterisk packages are unavailable; building pinned Asterisk $ASTERISK_SOURCE_VERSION."
        build_asterisk_from_source
    fi
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y \
        asterisk asterisk-devel autoconf automake gcc git make \
        alsa-lib-devel libsrtp-devel libtool sqlite-devel
else
    printf '%s\n' "Unsupported package manager. Install Asterisk headers, ALSA/sqlite development files, build tools, and git first." >&2
    exit 1
fi

ASTERISK_VERSION=$(asterisk -V | sed -n 's/^Asterisk \([0-9][0-9]*\(\.[0-9][0-9]*\)\{0,2\}\).*/\1/p')
if [ -z "$ASTERISK_VERSION" ]; then
    printf '%s\n' "Could not determine the installed Asterisk version." >&2
    exit 1
fi

srtp_module_found=false
for candidate in \
    /usr/lib/asterisk/modules/res_srtp.so \
    /usr/lib64/asterisk/modules/res_srtp.so \
    /usr/lib/*/asterisk/modules/res_srtp.so
do
    if [ -f "$candidate" ]; then
        srtp_module_found=true
        break
    fi
done
if [ "$srtp_module_found" != true ]; then
    printf '%s\n' "Asterisk was installed without res_srtp.so; refusing an unencrypted voice installation." >&2
    exit 1
fi

git clone "$DRIVER_REPOSITORY" "$VOICE_BUILD_DIR/chan-quectel"
cd "$VOICE_BUILD_DIR/chan-quectel"
git checkout --detach "$DRIVER_COMMIT"
git apply "$SCRIPT_DIR/patches/0001-defer-sms-during-active-call.patch"
./bootstrap
./configure --with-astversion="$ASTERISK_VERSION"
make
make install

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet asterisk.service; then
    systemctl stop asterisk.service
    printf '%s\n' "Stopped the distribution Asterisk service; EC25 Toolbox runs its own isolated instance."
fi

printf '%s\n' "Installed pinned sn4f/asterisk-chan-quectel commit $DRIVER_COMMIT with the SMS-during-call safety patch."
printf '%s\n' "Next: configure [voice], set calls.mode=\"bridge\", then run ./install.sh."
