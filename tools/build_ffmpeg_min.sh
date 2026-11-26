#!/usr/bin/env bash
# 自编译最小 ffmpeg（静态、LGPL），只保留本项目的合并/封装、截帧、片段、mp3 提取能力。
# 须在 MSYS2 MINGW64 环境运行，先安装：
#   pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-nasm mingw-w64-x86_64-lame \
#            mingw-w64-x86_64-zlib mingw-w64-x86_64-libxml2 mingw-w64-x86_64-pkgconf \
#            pkgconf make
# 用法：FFMPEG_BUILD_DIR=/e/envir/build/ffmpeg-min ./tools/build_ffmpeg_min.sh
set -euo pipefail

VERSION="8.1.3"
SHA256="7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3"
URL="https://ffmpeg.org/releases/ffmpeg-${VERSION}.tar.xz"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${FFMPEG_BUILD_DIR:-$HOME/ffmpeg-min-build}"
SRC_DIR="$BUILD_DIR/ffmpeg-$VERSION"
ARCHIVE="$BUILD_DIR/ffmpeg-$VERSION.tar.xz"
JOBS="${JOBS:-$(nproc)}"

if [ "${MSYSTEM:-}" != "MINGW64" ]; then
  echo "请在 MSYS2 MINGW64 终端运行（MSYSTEM=MINGW64）" >&2
  exit 1
fi
for tool in gcc nasm pkg-config make; do
  command -v "$tool" >/dev/null || { echo "缺少 $tool" >&2; exit 1; }
done
pkg-config --exists lame || { echo "缺少 libmp3lame（lame.pc）" >&2; exit 1; }
# MSYS2 的 libmp3lame.a 内嵌了 mpg123 解码接口，静态链接时必须显式补上
pkg-config --exists libmpg123 || { echo "缺少 libmpg123" >&2; exit 1; }
MPG123_LIBS="$(pkg-config --static --libs libmpg123)"
pkg-config --exists libxml-2.0 || { echo "缺少 libxml2" >&2; exit 1; }

mkdir -p "$BUILD_DIR"
if [ ! -f "$ARCHIVE" ]; then
  echo "下载 $URL"
  curl -L --retry 3 -o "$ARCHIVE" "$URL"
fi
echo "$SHA256 *$ARCHIVE" | sha256sum -c -
if [ ! -d "$SRC_DIR" ]; then
  tar -C "$BUILD_DIR" -xf "$ARCHIVE"
fi

cd "$SRC_DIR"
make distclean >/dev/null 2>&1 || true
./configure \
  --arch=x86_64 --target-os=mingw32 \
  --enable-static --disable-shared \
  --disable-everything \
  --enable-ffmpeg --disable-ffplay --disable-ffprobe \
  --disable-doc --disable-debug --disable-autodetect --enable-small \
  --extra-ldflags=-static \
  --enable-network --enable-zlib --enable-schannel \
  --enable-libmp3lame --enable-libxml2 \
  --extra-libs="$MPG123_LIBS" \
  --pkg-config-flags=--static \
  --enable-protocol=file,pipe,http,https,tcp,tls,crypto,data \
  --enable-demuxer=mov,matroska,hls,dash,flv,live_flv,mpegts,aac,mp3,ogg,wav,flac,image2,concat,srt,ass,webvtt,avi \
  --enable-muxer=mov,mp4,ipod,matroska,webm,mp3,ogg,wav,flac,image2,null \
  --enable-parser=h264,hevc,mpeg4video,vp3,vp8,vp9,av1,aac,aac_latm,mpegaudio,opus,vorbis,flac,ac3,mpegvideo \
  --enable-bsf=aac_adtstoasc,h264_mp4toannexb,hevc_mp4toannexb,vp9_superframe,extract_extradata,setts \
  --enable-encoder=libmp3lame,mjpeg,png,movtext,srt,ass,webvtt \
  --enable-decoder=h264,hevc,mpeg4,mpeg2video,vp3,vp8,vp9,av1,flv,mjpeg,png,webp,aac,mp3,opus,vorbis,flac,ac3,eac3,pcm_s16le,pcm_s24le,subrip,ass,webvtt,movtext \
  --enable-filter=scale,format,aresample,aformat,null,anull \
  --enable-swscale --enable-swresample

make -j"$JOBS"
strip -s ffmpeg.exe
cp -f ffmpeg.exe "$REPO_ROOT/ffmpeg.exe"

echo "---"
"$REPO_ROOT/ffmpeg.exe" -version | head -1
ls -l "$REPO_ROOT/ffmpeg.exe"