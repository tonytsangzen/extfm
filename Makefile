# Ext 文件系统浏览器
# E2FS 指向 e2fsprogs 构建树（含 lib/*.a 与 misc/、e2fsck/ 可执行）。
# 默认优先使用本机常见位置，否则回退到 ~/e2fsprogs（CI 中克隆到该处）。
E2FS      ?= $(shell test -f /Users/tony/work/e2fsprogs/lib/libext2fs.a \
              && echo /Users/tony/work/e2fsprogs || echo $(HOME)/e2fsprogs)
E2FS_LIB  := $(E2FS)/lib
CC        ?= cc
PYTHON    ?= python3

CFLAGS    := -O2 -Wall -fPIC \
             -I$(E2FS_LIB) -I$(E2FS_LIB)/ext2fs -I$(E2FS_LIB)/et \
             -I$(E2FS_LIB)/uuid -Isrc
LDLIBS    := -L$(E2FS_LIB) -lext2fs -lcom_err -luuid -le2p

BUILD     := build

.PHONY: all test images webview clean run

all: $(BUILD)/libe2fsbridge.dylib $(BUILD)/bridge_test $(BUILD)/write_test $(BUILD)/E2fsBrowser

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/libe2fsbridge.dylib: src/bridge.c src/bridge.h | $(BUILD)
	$(CC) $(CFLAGS) -dynamiclib -o $@ src/bridge.c $(LDLIBS)

$(BUILD)/bridge_test: src/bridge_test.c $(BUILD)/libe2fsbridge.dylib
	$(CC) $(CFLAGS) -o $@ src/bridge_test.c \
		-L$(BUILD) -le2fsbridge -rpath @executable_path

$(BUILD)/write_test: src/write_test.c $(BUILD)/libe2fsbridge.dylib
	$(CC) $(CFLAGS) -o $@ src/write_test.c \
		-L$(BUILD) -le2fsbridge -rpath @executable_path

$(BUILD)/E2fsBrowser: webview/main.swift | $(BUILD)
	swiftc -O -o $@ webview/main.swift -framework Cocoa -framework WebKit

# 生成测试镜像（ext2/ext3/ext4）
images: $(BUILD)/libe2fsbridge.dylib
	bash tests/make_images.sh

# 桥接层冒烟测试
test: $(BUILD)/bridge_test | images
	$(BUILD)/bridge_test images/ext4.img

# 写操作测试（在临时镜像上执行，随后 e2fsck 校验一致性）
test-write: $(BUILD)/write_test | images
	cp images/ext4.img $(BUILD)/wtest.img
	$(BUILD)/write_test $(BUILD)/wtest.img
	$(E2FS)/e2fsck/e2fsck -fn $(BUILD)/wtest.img

# 直接以 web 模式启动（浏览器访问）
run: all
	./run.sh --web

clean:
	rm -rf $(BUILD)
