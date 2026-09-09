#!/bin/bash
# tests/make_images.sh — 用 e2fsprogs 的 mke2fs/debugfs 制作测试镜像
set -e
cd "$(dirname "$0")/.."

E2FS=${E2FS:-/Users/tony/work/e2fsprogs}
MKE2FS=$E2FS/misc/mke2fs
DEBUGFS=$E2FS/debugfs/debugfs
TMP=tmp
mkdir -p images $TMP

# ---------- 本地素材 ----------
cat > "$TMP/说明.txt" <<'EOF'
这是 Ext 文件系统浏览器 的测试文件。
The quick brown fox jumps over the lazy dog. 0123456789
测试 UTF-8 中文内容、以及 行尾 \n 的保留。
EOF

python3 - "$TMP" <<'EOF'
import zlib, struct, sys, os
tmp = sys.argv[1]
def chunk(t, d):
    return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
def png(path, w, h, rgb):
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))
    data = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
    open(path, 'wb').write(data)
png(os.path.join(tmp, 'logo.png'), 96, 96, (30, 144, 255))
png(os.path.join(tmp, 'grid.png'), 200, 120, (240, 128, 96))
EOF

dd if=/dev/urandom of=$TMP/big.bin bs=1024 count=4096 2>/dev/null   # 4MB
printf 'plain text head\x00\x01\x02\x00binary tail' > "$TMP/data.bin"

populate() {
    local img=$1
    $DEBUGFS -w -f - "$img" <<EOF
mkdir /docs
cd /docs
write $TMP/说明.txt 说明.txt
write $TMP/logo.png 截图.png
mkdir 子目录
cd /
mkdir /nested
cd /nested
mkdir deep
cd /
write $TMP/big.bin big.bin
write $TMP/data.bin data.bin
symlink /软链接.txt /docs/说明.txt
ln /docs/说明.txt /hardlink.txt
sif /docs/说明.txt links_count 2
quit
EOF
}

echo "== ext4 =="
rm -f images/ext4.img
dd if=/dev/zero of=images/ext4.img bs=1m count=64 2>/dev/null
$MKE2FS -q -t ext4 -b 4096 -I 256 -L E2FS-TEST -F images/ext4.img
populate images/ext4.img

echo "== ext2 =="
rm -f images/ext2.img
dd if=/dev/zero of=images/ext2.img bs=1m count=32 2>/dev/null
$MKE2FS -q -t ext2 -b 1024 -L E2FS-OLD -F images/ext2.img
populate images/ext2.img

echo "== ext3 =="
rm -f images/ext3.img
dd if=/dev/zero of=images/ext3.img bs=1m count=32 2>/dev/null
$MKE2FS -q -t ext3 -b 1024 -L E2FS-JRN -F images/ext3.img
populate images/ext3.img

echo "== not-ext (随机数据，用于错误处理测试) =="
dd if=/dev/urandom of=images/notext.img bs=1m count=2 2>/dev/null

ls -la images/
echo "done."
