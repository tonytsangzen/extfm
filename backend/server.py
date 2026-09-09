#!/usr/bin/env python3
"""
server.py — Ext 文件系统浏览器后端。

通过 ctypes 调用 build/libe2fsbridge.dylib（封装 e2fsprogs libext2fs），
对外提供 REST API 与静态前端页面。仅绑定 127.0.0.1，只读访问。
"""
import argparse
import ctypes
import json
import mimetypes
import os
import plistlib
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, quote

from pending import PendingFS

BASE = Path(__file__).resolve().parent.parent
DEFAULT_BRIDGE = BASE / "build" / "libe2fsbridge.dylib"
DEFAULT_FRONTEND = BASE / "frontend"

CHUNK = 256 * 1024


# --------------------------------------------------------------------------- #
# 桥接库封装
# --------------------------------------------------------------------------- #
class BridgeError(Exception):
    pass


class Bridge:
    """libe2fsbridge.dylib 的 ctypes 封装（内部串行化，libext2fs 句柄非线程安全）"""

    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.lib = ctypes.CDLL(str(path))
        lib = self.lib

        c2 = [ctypes.POINTER(ctypes.c_void_p)] * 2
        lib.e2b_open.argtypes = [ctypes.c_char_p] + c2
        lib.e2b_open.restype = ctypes.c_int
        lib.e2b_open_ro.argtypes = [ctypes.c_char_p] + c2
        lib.e2b_open_ro.restype = ctypes.c_int
        lib.e2b_info.argtypes = c2
        lib.e2b_info.restype = ctypes.c_int
        lib.e2b_list.argtypes = [ctypes.c_char_p] + c2
        lib.e2b_list.restype = ctypes.c_int
        lib.e2b_stat.argtypes = [ctypes.c_char_p] + c2
        lib.e2b_stat.restype = ctypes.c_int
        lib.e2b_probe.argtypes = [ctypes.c_char_p] + c2
        lib.e2b_probe.restype = ctypes.c_int
        lib.e2b_file_read.argtypes = [
            ctypes.c_char_p, ctypes.c_longlong, ctypes.c_longlong,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_longlong),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.e2b_file_read.restype = ctypes.c_int
        lib.e2b_close.argtypes = []
        lib.e2b_close.restype = ctypes.c_int
        lib.e2b_is_open.argtypes = []
        lib.e2b_is_open.restype = ctypes.c_int
        lib.e2b_writable.argtypes = []
        lib.e2b_writable.restype = ctypes.c_int
        lib.e2b_mkdir.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.e2b_mkdir.restype = ctypes.c_int
        lib.e2b_rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                   ctypes.POINTER(ctypes.c_void_p)]
        lib.e2b_rename.restype = ctypes.c_int
        lib.e2b_copy.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                 ctypes.POINTER(ctypes.c_void_p)]
        lib.e2b_copy.restype = ctypes.c_int
        lib.e2b_free.argtypes = [ctypes.c_void_p]
        lib.e2b_free.restype = None
        lib.e2b_version.argtypes = []
        lib.e2b_version.restype = ctypes.c_char_p

    # -- 内部 ---------------------------------------------------------------
    def _take(self, p):
        if not p:
            return None
        data = ctypes.string_at(p)
        self.lib.e2b_free(p)
        return data

    def _json_call(self, fn, *args):
        """调用形如 (…, char **out_json, char **out_err) 的桥接函数"""
        jo = ctypes.c_void_p()
        je = ctypes.c_void_p()
        with self.lock:
            rc = fn(*args, ctypes.byref(jo), ctypes.byref(je))
        err = self._take(je)
        if rc != 0:
            raise BridgeError(err.decode("utf-8", "replace") if err else f"error {rc}")
        out = self._take(jo)
        if out is None:
            raise BridgeError("empty response from bridge")
        return json.loads(out.decode("utf-8", "replace"))

    def _err_call(self, fn, *args):
        """调用形如 (…, char **out_err) 的桥接函数"""
        je = ctypes.c_void_p()
        with self.lock:
            rc = fn(*args, ctypes.byref(je))
        err = self._take(je)
        if rc != 0:
            raise BridgeError(err.decode("utf-8", "replace") if err else f"error {rc}")

    # -- 对外 ---------------------------------------------------------------
    def version(self) -> str:
        with self.lock:
            return self.lib.e2b_version().decode()

    def writable(self) -> bool:
        with self.lock:
            return bool(self.lib.e2b_writable())

    def reopen_after_job(self, device: str, was_writable: bool):
        """任务结束后重新打开文件系统；失败时保持关闭状态。
        注意：不清空 pending（saveas 需要保留当前会话的缓冲）"""
        try:
            self.open(device, writable=was_writable)
        except BridgeError:
            try:
                self.open(device, writable=False)
            except BridgeError:
                pass
        if app_state["pending"] is None:
            app_state["pending"] = PendingFS(app_state["buffer_limit"])

    def mkdir(self, path: str):
        self._err_call(self.lib.e2b_mkdir, os.fsencode(path))

    def rename(self, old: str, new: str):
        self._err_call(self.lib.e2b_rename, os.fsencode(old), os.fsencode(new))

    def copy(self, old: str, new: str):
        self._err_call(self.lib.e2b_copy, os.fsencode(old), os.fsencode(new))

    def open(self, device: str, writable: bool = True) -> dict:
        fn = self.lib.e2b_open if writable else self.lib.e2b_open_ro
        return self._json_call(fn, os.fsencode(device))

    def close(self):
        with self.lock:
            self.lib.e2b_close()

    def is_open(self) -> bool:
        with self.lock:
            return bool(self.lib.e2b_is_open())

    def info(self) -> dict:
        return self._json_call(self.lib.e2b_info)

    def list(self, path: str) -> dict:
        return self._json_call(self.lib.e2b_list, os.fsencode(path))

    def stat(self, path: str) -> dict:
        return self._json_call(self.lib.e2b_stat, os.fsencode(path))

    def probe(self, device: str) -> dict:
        return self._json_call(self.lib.e2b_probe, os.fsencode(device))

    def read(self, path: str, offset: int, length: int) -> bytes:
        buf = (ctypes.c_char * length)()
        got = ctypes.c_longlong(0)
        je = ctypes.c_void_p()
        with self.lock:
            rc = self.lib.e2b_file_read(
                os.fsencode(path), offset, length, buf,
                ctypes.byref(got), ctypes.byref(je))
        err = self._take(je)
        if rc != 0:
            raise BridgeError(err.decode("utf-8", "replace") if err else f"error {rc}")
        return bytes(buf[: got.value])


# --------------------------------------------------------------------------- #
# 块设备：diskutil 拓扑 / 授权 / 挂载
# --------------------------------------------------------------------------- #
DEV_PATH_RE = re.compile(r"^/dev/(r?disk[0-9]+(s[0-9]+)*)$")
# 内置盘上无需展示的系统专用分区
APPLE_SYSTEM_CONTENTS = {"Apple_APFS_ISC", "Apple_APFS_Recovery",
                         "Apple_APFS", "Apple_Boot", "Apple_Recovery"}


def run_diskutil(*args, timeout=25):
    """执行 diskutil；不可用（非 macOS）返回 None"""
    try:
        return subprocess.run(["/usr/sbin/diskutil", *args],
                              capture_output=True, text=False, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _disk_info(devid: str) -> dict:
    r = run_diskutil("info", "-plist", devid, timeout=15)
    if r is None or r.returncode != 0:
        return {}
    try:
        p = plistlib.loads(r.stdout)
    except plistlib.InvalidFileException:
        return {}
    return {
        "removable": bool(p.get("RemovableMediaOrExternalDevice")),
        "internal": p.get("Internal"),
        "virtual": p.get("VirtualOrPhysical") == "Virtual",
        "ejectable": bool(p.get("Ejectable")),
        "media_name": p.get("MediaName") or "",
        "bus": p.get("BusProtocol") or "",
    }


_TOPO_CACHE = {"t": 0.0, "data": {}}


def invalidate_topo_cache():
    _TOPO_CACHE["t"] = 0.0


def disk_topology_cached(ttl: float = 3.0) -> dict:
    import time as _time
    now = _time.time()
    if now - _TOPO_CACHE["t"] > ttl:
        _TOPO_CACHE["data"] = disk_topology()
        _TOPO_CACHE["t"] = now
    return _TOPO_CACHE["data"]


def device_node_exists(dev: str) -> bool:
    """设备/文件是否真实存在。
    /dev 节点在设备被强制移除后会残留为陈旧节点（os.path.exists 仍为 True），
    需要结合磁盘拓扑或实际打开探测判断。"""
    if not dev.startswith("/dev/"):
        return os.path.exists(dev)
    if not os.path.exists(dev):
        return False
    if dev in disk_topology_cached():
        return True
    import errno as _errno
    try:
        fd = os.open(dev, os.O_RDONLY)
        os.close(fd)
        return True
    except OSError as e:
        return e.errno in (_errno.EPERM, _errno.EACCES, _errno.EBUSY)


def disk_topology() -> dict:
    """macOS 磁盘拓扑：{"/dev/diskXsY": 元数据}。非 macOS 返回 {}。"""
    r = run_diskutil("list", "-plist")
    if r is None or r.returncode != 0:
        return {}
    try:
        pl = plistlib.loads(r.stdout)
    except plistlib.InvalidFileException:
        return {}

    out = {}
    for d in pl.get("AllDisksAndPartitions", []):
        parent = d.get("DeviceIdentifier") or ""
        if not parent:
            continue
        info = _disk_info(parent)
        if d.get("OSInternal") or (info.get("virtual") and info.get("internal")):
            continue    # 系统合成容器（iSCPreboot/Recovery/系统卷），对本工具无意义
        parts = list(d.get("Partitions") or []) + list(d.get("APFSVolumes") or [])
        if not parts:
            parts = [d]
        for p in parts:
            devid = p.get("DeviceIdentifier")
            if not devid:
                continue
            content = p.get("Content") or d.get("Content") or ""
            # 内置盘上的 Apple 系统专用分区不展示
            if info.get("internal") and content in APPLE_SYSTEM_CONTENTS:
                continue
            mp = p.get("MountPoint") or ""
            out[f"/dev/{devid}"] = {
                "parent": parent,
                "removable": bool(info.get("removable")),
                "ejectable": bool(info.get("ejectable")),
                "internal": info.get("internal"),
                "media_name": info.get("media_name", ""),
                "bus": info.get("bus", ""),
                "content": content,
                "volume_name": p.get("VolumeName") or "",
                "mounted": bool(mp),
                "mount_point": mp,
                "size": p.get("Size") or p.get("DiskSize") or 0,
            }
    return out


# --------------------------------------------------------------------------- #
# 设备 / 镜像扫描
# --------------------------------------------------------------------------- #
IMAGE_EXTS = {".img", ".raw", ".dd", ".ext2", ".ext3", ".ext4",
              ".vhd", ".vhdx", ".vmdk", ".qcow2"}
SCAN_DIRS = ["Desktop", "Downloads", "Documents", "Pictures"]


def scan_candidates() -> dict:
    """扫描块设备（含可移动设备与挂载状态）与常见目录下的磁盘镜像文件。"""
    bridge: Bridge = app_state["bridge"]
    devices, images, skipped = [], [], []

    topo = disk_topology()
    if topo:
        # macOS：以 diskutil 拓扑为准（已过滤系统合成卷/系统分区）
        def sort_key(p):
            m = topo[p]
            return (0 if m.get("removable") else 1, p)
        targets = sorted(topo.keys(), key=sort_key)
        for path in targets:
            m = topo[path]
            try:
                r = bridge.probe(path)
            except BridgeError as e:
                skipped.append({"path": path, "error": str(e)})
                continue
            r["kind"] = "device"
            for k in ("removable", "ejectable", "internal", "media_name",
                      "bus", "content", "volume_name", "mounted",
                      "mount_point", "parent"):
                r[k] = m.get(k)
            r["size"] = m.get("size") or r.get("capacity") or 0
            if r.get("ok"):
                devices.append(r)
            elif "not permitted" in r.get("error", "").lower() or \
                    "permission denied" in r.get("error", "").lower():
                r["denied"] = True
                devices.append(r)
            else:
                # 可读但不是 ext（FAT/HFS/NTFS 等）——仍然列出以提供挂载选项
                devices.append(r)
    else:
        # 非 macOS 回退：直接探测 /dev 下的常见块设备名
        dev = Path("/dev")
        patterns = ["disk*", "sd*", "nvme*n*", "mmcblk*", "vd*"]
        names = sorted({p.name for pat in patterns for p in dev.glob(pat)}) \
            if dev.is_dir() else []
        for name in names:
            path = str(dev / name)
            try:
                r = bridge.probe(path)
            except BridgeError as e:
                skipped.append({"path": path, "error": str(e)})
                continue
            r["kind"] = "device"
            if r.get("ok"):
                devices.append(r)
            elif "not permitted" in r.get("error", "").lower() or \
                    "permission denied" in r.get("error", "").lower():
                r["denied"] = True
                devices.append(r)
            else:
                skipped.append(r)

    # 镜像文件
    roots = []
    home = Path.home()
    roots += [home / d for d in SCAN_DIRS]
    roots += [BASE / "images", Path.cwd()]
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for p in entries:
            if not p.is_file() or p.name in seen:
                continue
            if p.suffix.lower() in IMAGE_EXTS or root == BASE / "images":
                seen.add(p.name)
                try:
                    r = bridge.probe(str(p))
                except BridgeError:
                    continue
                r["kind"] = "image"
                if r.get("ok"):
                    images.append(r)

    return {"devices": devices, "images": images, "skipped": skipped[:20]}


# --------------------------------------------------------------------------- #
# 写操作辅助
# --------------------------------------------------------------------------- #
def join_unix(parent: str, name: str) -> str:
    return (parent.rstrip("/") or "") + "/" + name


def unique_name(existing: set, base: str, suffix: str | None = None) -> str:
    """目标目录已有同名项时自动改名：copy -> “xxx 副本”；move -> “xxx 2”"""
    if base not in existing:
        return base
    if "." in base[1:]:
        stem, ext = base.rsplit(".", 1)
        ext = "." + ext
    else:
        stem, ext = base, ""
    if suffix:
        cand = f"{stem} {suffix}{ext}"
        if cand not in existing:
            return cand
        n = 2
        while f"{stem} {suffix} {n}{ext}" in existing:
            n += 1
        return f"{stem} {suffix} {n}{ext}"
    n = 2
    while f"{stem} {n}{ext}" in existing:
        n += 1
    return f"{stem} {n}{ext}"


OPEN_TEMP_ROOT = f"/tmp/e2fs-open-{os.getpid()}"


def open_with_system(bridge: Bridge, path: str):
    """把文件提取到临时目录后交给系统默认应用打开（缓冲中的文件从内存提取）"""
    st = fs_stat(path)
    res = st.get("resolved") or st.get("raw") or {}
    if res.get("type") not in ("file", "symlink", "unknown"):
        return {"ok": False, "error": "只能打开常规文件"}
    os.makedirs(OPEN_TEMP_ROOT, exist_ok=True)
    size = int(res.get("size", 0))
    out = os.path.join(OPEN_TEMP_ROOT, os.path.basename(path.rstrip("/")) or "file")
    p: PendingFS = app_state["pending"]
    blob = None
    real_path = path
    if p is not None:
        kind, v = p.read_bytes(path)
        if kind == "missing":
            return {"ok": False, "error": "路径不存在"}
        if kind == "blob":
            blob = v
        else:
            real_path = v
    with open(out, "wb") as f:
        if blob is not None:
            f.write(blob[:size] if size < len(blob) else blob)
        else:
            off = 0
            while off < size:
                data = bridge.read(real_path, off, min(1 << 20, size - off))
                if not data:
                    break
                f.write(data)
                off += len(data)
    import shlex
    import subprocess
    cmd = os.environ.get("E2FS_OPEN_CMD")
    argv = shlex.split(cmd) + [out] if cmd else ["open", out]
    try:
        subprocess.run(argv, check=False, timeout=30)
    except FileNotFoundError:
        return {"ok": False, "error": f"找不到打开命令: {argv[0]}"}
    return {"ok": True, "path": path, "tmp": out}


# --------------------------------------------------------------------------- #
# 缓冲视图 / 写回
# --------------------------------------------------------------------------- #
def fs_stat(path: str) -> dict:
    """overlay 感知的 stat：缓冲区有则返回内存版本"""
    p = app_state["pending"]
    if p is not None:
        try:
            return p.stat(path, app_state["bridge"])
        except KeyError:
            raise BridgeError(f"路径不存在: {path}")
    return app_state["bridge"].stat(path)


def fs_list(path: str) -> dict:
    """overlay 感知的目录列表"""
    p = app_state["pending"]
    if p is not None:
        try:
            entries = p.list_dir(path, app_state["bridge"])
        except KeyError:
            raise BridgeError(f"目录不存在: {path}")
        except BridgeError:
            raise
        return {"path": path, "count": len(entries), "entries": entries}
    return app_state["bridge"].list(path)


def do_flush() -> dict:
    """把缓冲的操作按原顺序重放写回磁盘；全部成功后清空缓冲"""
    p = app_state["pending"]
    bridge = app_state["bridge"]
    if p is None or not p.count:
        return {"ok": True, "flushed": 0}
    ops = list(p.ops)
    flushed = 0
    for i, op in enumerate(ops):
        try:
            if op["op"] == "mkdir":
                bridge.mkdir(op["path"])
            elif op["op"] == "rename":
                bridge.rename(op["old"], op["new"])
            elif op["op"] == "copy":
                bridge.copy(op["old"], op["new"])
        except BridgeError as e:
            # 磁盘已应用 ops[0..i)：在当前磁盘状态上重建剩余操作的 overlay
            remaining = ops[i:]
            p.reset()
            for op2 in remaining:
                if op2["op"] == "mkdir":
                    p.add_mkdir(op2["path"])
                elif op2["op"] == "rename":
                    meta = None
                    kind, v = p.resolve(op2["old"])
                    if kind == "real":
                        try:
                            meta = bridge.stat(v)
                        except BridgeError:
                            meta = None
                    p.add_rename(op2["old"], op2["new"], meta)
                else:
                    try:
                        meta = p.stat(op2["old"], bridge)
                        res = meta.get("resolved") or {}
                        data = None
                        if not meta.get("is_symlink") and \
                                res.get("type") == "file":
                            kind, v = p.resolve(op2["old"])
                            if kind == "blob":
                                data = p.blobs[v]
                            else:
                                data = bridge.read(v, 0,
                                                   int(res.get("size", 0)))
                        p.add_copy(op2["old"], op2["new"], meta, data)
                    except (BridgeError, KeyError):
                        continue    # 源已不可达，丢弃该操作
            return {"ok": False, "flushed": flushed, "error": str(e)}
        flushed += 1
    p.reset()
    return {"ok": True, "flushed": flushed}


# --------------------------------------------------------------------------- #
# 维护任务：文件系统检查 / 格式化 / 保存到其他磁盘或文件
# --------------------------------------------------------------------------- #
JOBS: dict = {}
JOBS_LOCK = threading.Lock()


def find_tool(tool: str):
    """定位 e2fsprogs 工具：环境变量 → 源码树 → PATH"""
    env = os.environ.get(f"{tool.upper()}_BIN")
    if env and os.path.exists(env):
        return env
    root = os.environ.get("E2FSPROGS_BIN", str(Path.home() / "e2fsprogs"))
    for cand in (f"{root}/{tool}/{tool}", f"{root}/misc/{tool}"):
        if os.path.exists(cand):
            return cand
    import shutil
    return shutil.which(tool)


def start_job(kind: str, fn) -> dict:
    """fn(progress_cb) -> (returncode, output)；后台线程执行，GET /api/job 轮询"""
    import uuid as _uuid
    jid = _uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {"id": jid, "kind": kind, "status": "running",
                     "output": "", "returncode": None, "progress": None}
    with JOBS_LOCK:
        job = JOBS[jid]

    def progress_cb(done, total):
        with JOBS_LOCK:
            job["progress"] = {"done": done, "total": total}

    def worker():
        try:
            rc, out = fn(progress_cb)
        except Exception as e:  # noqa: BLE001
            rc, out = -1, f"{e}"
        with JOBS_LOCK:
            job["status"] = "done" if rc == 0 else "error"
            job["returncode"] = rc
            if out:
                job["output"] = (job["output"] + "\n" + out).strip()
    threading.Thread(target=worker, daemon=True).start()
    return job


def tool_check_job(repair: bool) -> dict:
    bridge: Bridge = app_state["bridge"]
    if not bridge.is_open():
        return self_error("尚未打开文件系统")
    r = do_flush()
    if not r.get("ok"):
        return self_error(f"写回失败: {r.get('error')}")
    device = bridge.info()["device"]
    was_writable = bridge.writable()
    tool = app_state["tools"].get("e2fsck")
    if not tool:
        return self_error("找不到 e2fsck（可通过 E2FSPROGS_BIN 指定源码树）")
    if repair:
        bridge.close()
        app_state["pending"] = PendingFS(app_state["buffer_limit"])
    args = [tool, "-f"] + (["-y"] if repair else ["-n"]) + [device]

    def fn(progress_cb=None):
        with bridge.lock:
            out = ""
            try:
                rc_ = subprocess.run(args, capture_output=True, text=True,
                                     timeout=1800)
                out = ((rc_.stdout or "") + (rc_.stderr or "")).strip()
                rc = rc_.returncode
            except subprocess.TimeoutExpired:
                rc, out = 124, "检查超时"
            except FileNotFoundError:
                rc, out = 127, f"找不到 {args[0]}"
            if repair and not bridge.is_open():
                # 修复后重新打开文件系统
                try:
                    bridge.open(device, writable=was_writable)
                    app_state["pending"] = PendingFS(app_state["buffer_limit"])
                    out += f"\n[已重新打开 {device}]"
                except BridgeError as e:
                    out += f"\n[重新打开失败: {e}]"
            return rc, out

    return start_job("check", fn)


def format_job(opts: dict) -> dict:
    bridge: Bridge = app_state["bridge"]
    target = os.path.normpath(opts.get("target") or "")
    if not target.startswith("/"):
        return self_error("目标路径必须是绝对路径")
    is_dev = target.startswith("/dev/")
    if is_dev and not DEV_PATH_RE.match(target):
        return self_error(f"非法设备路径: {target}")
    fstype = opts.get("type") or "ext4"
    if fstype not in ("ext2", "ext3", "ext4"):
        return self_error("类型仅支持 ext2/ext3/ext4")
    tool = app_state["tools"].get("mke2fs")
    if not tool:
        return self_error("找不到 mke2fs（可通过 E2FSPROGS_BIN 指定源码树）")

    # 目标为当前打开的文件系统：先写回并关闭，格式化后重新打开
    is_current = bridge.is_open() and \
        os.path.normpath(bridge.info()["device"]) == target
    if is_current:
        r = do_flush()
        if not r.get("ok"):
            return self_error(f"写回失败: {r.get('error')}")
        bridge.close()

    if opts.get("new_file"):
        d = os.path.dirname(target)
        if not os.path.isdir(d):
            return self_error(f"目录不存在: {d}")
        if os.path.exists(target) and not opts.get("force"):
            return self_error("目标文件已存在")
        with open(target, "wb") as f:
            f.truncate(int(opts.get("size_mb", 64)) * 1024 * 1024)

    args = [tool, "-F", "-t", fstype, "-q"]
    bs = opts.get("block_size")
    if bs in (1024, 2048, 4096):
        args += ["-b", str(bs)]
    if opts.get("label"):
        args += ["-L", str(opts["label"])[:16]]
    if opts.get("uuid"):
        args += ["-U", str(opts["uuid"])]
    if opts.get("inode_size") in (128, 256, 512):
        args += ["-I", str(opts["inode_size"])]
    rp = opts.get("reserved_pct")
    if rp is not None and rp != "":
        args += ["-m", str(max(0, min(50, int(rp))))]
    feats = [f for f in (opts.get("features") or [])
             if re.match(r"^[A-Za-z0-9_^]+$", str(f))]
    if feats:
        args += ["-O", ",".join(feats)]
    args.append(target)

    def fn(progress_cb=None):
        with bridge.lock:
            try:
                r = subprocess.run(args, capture_output=True, text=True,
                                   timeout=1800)
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                rc = r.returncode
            except subprocess.TimeoutExpired:
                rc, out = 124, "格式化超时"
            except FileNotFoundError:
                rc, out = 127, f"找不到 {args[0]}"
            if is_current:
                try:
                    fs = bridge.open(target, writable=not is_dev)
                    app_state["pending"] = PendingFS(app_state["buffer_limit"])
                    out += f"\n[已重新打开 {target} ({fs.get('fstype')}, " \
                           f"标签 {fs.get('label') or '无'})]"
                except BridgeError as e:
                    out += f"\n[重新打开失败: {e}]"
            return rc, out

    return start_job("format", fn)


def save_job(opts: dict) -> dict:
    bridge: Bridge = app_state["bridge"]
    if not bridge.is_open():
        return self_error("尚未打开文件系统")
    target = os.path.normpath(opts.get("target") or "")
    mode = opts.get("mode") or "clone"
    if mode not in ("clone", "saveas"):
        return self_error("mode 仅支持 clone / saveas")
    is_dev_target = target.startswith("/dev/")
    if is_dev_target and not DEV_PATH_RE.match(target):
        return self_error(f"非法设备路径: {target}")
    if not is_dev_target:
        d = os.path.dirname(target)
        if not os.path.isdir(d):
            return self_error(f"目录不存在: {d}")
        if os.path.exists(target) and not opts.get("force"):
            return self_error("目标文件已存在（可勾选覆盖）")
    info = bridge.info()
    src = info["device"]
    total = int(info.get("capacity") or 0)
    was_writable = bridge.writable()
    pending = app_state["pending"]

    # clone：先把缓冲写回源盘，再做完整克隆；saveas：缓冲只写入副本，原盘不动
    if mode == "clone":
        r = do_flush()
        if not r.get("ok"):
            return self_error(f"写回失败: {r.get('error')}")

    def fn(progress_cb=None):
        with bridge.lock:
            out = ""
            if mode == "saveas" and bridge.is_open():
                bridge.close()      # 释放源，克隆期间不占用
            try:
                srcfd = os.open(src, os.O_RDONLY)
            except OSError as e:
                bridge.reopen_after_job(src, was_writable)
                return 1, f"读取源失败: {e}（可能需要授权读取该设备）"
            try:
                flags = os.O_WRONLY | os.O_CREAT
                flags |= os.O_TRUNC if opts.get("force") else os.O_EXCL
                try:
                    dstfd = os.open(target, flags, 0o644)
                except OSError as e:
                    os.close(srcfd)
                    return 1, f"写入目标失败: {e}"
                done = 0
                err = None
                while done < total:
                    n = min(1 << 20, total - done)
                    try:
                        buf = os.pread(srcfd, n, done)
                    except OSError as e:
                        err = f"读取失败 @ {done}: {e}"
                        break
                    if not buf:
                        break
                    offw = 0
                    while offw < len(buf):
                        offw += os.write(dstfd, buf[offw:])
                    done += len(buf)
                    if progress_cb:
                        progress_cb(done, total)
                os.close(dstfd)
                if err:
                    out = err
                    rc = 1
                else:
                    rc = 0
                    out = f"已保存 {done} 字节到 {target}"
            finally:
                os.close(srcfd)
            # saveas：把缓冲的重放施加到副本上，然后恢复打开源盘
            if mode == "saveas":
                try:
                    bridge.open(target, writable=True)
                    for op in list(pending.ops):
                        if op["op"] == "mkdir":
                            bridge.mkdir(op["path"])
                        elif op["op"] == "rename":
                            bridge.rename(op["old"], op["new"])
                        elif op["op"] == "copy":
                            bridge.copy(op["old"], op["new"])
                    out += f"\n[缓冲的 {pending.count} 项改动已写入副本]"
                    bridge.close()
                except BridgeError as e:
                    out += f"\n[写入副本时出错: {e}]"
                bridge.reopen_after_job(src, was_writable)
        return rc, out

    return start_job("save", fn)


def self_error(msg: str):
    raise BridgeError(msg)


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "E2fsBrowser/1.0"

    # -- 基础工具 -------------------------------------------------------------
    def log_message(self, fmt, *args):  # 只记录非 2xx
        line = fmt % args
        if " 200 " not in line and " 304 " not in line:
            sys.stderr.write("[http] %s\n" % line)

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_error_json(self, msg, status=400):
        self.send_json({"ok": False, "error": msg}, status)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def q1(self, key):
        """取查询参数。兼容未经百分号编码的原始 UTF-8 字节（latin-1 恢复），
        并保留文件名中的 '+'。"""
        q = urlparse(self.path).query
        try:
            q = q.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        for part in q.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k == key:
                    return unquote(v)
        return None

    # -- 路由 -----------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route == "/":
                return self.serve_static("index.html")
            if route.startswith("/api/"):
                return self.handle_api(route, parsed)
            return self.serve_static(route.lstrip("/"))
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[error] {route}: {e!r}\n")
            try:
                self.send_error_json(f"服务器内部错误: {e}", 500)
            except Exception:
                pass

    def do_POST(self):
        self.do_GET()

    def do_HEAD(self):
        self.do_GET()

    # -- API ------------------------------------------------------------------
    def handle_api(self, route, parsed):
        try:
            return self._handle_api(route, parsed)
        except BridgeError as e:
            return self.send_error_json(str(e), 400)

    def handle_write(self, route):
        """mkdir / rename / copy / move —— 只写入内存缓冲，不直接碰磁盘"""
        bridge: Bridge = app_state["bridge"]
        p: PendingFS = app_state["pending"]
        if p is None:
            return self.send_error_json("尚未打开文件系统", 409)
        body = self.read_body() if self.command == "POST" else {}

        def finish(extra):
            flushed = False
            if p.buffered_bytes() >= p.limit:
                r = do_flush()
                if not r.get("ok"):
                    return self.send_error_json(
                        f"内存缓冲达到上限，但自动写回失败: {r.get('error')}", 409)
                flushed = True
            return self.send_json({"ok": True, "flushed": flushed,
                                   "pending": p.info(), **extra})

        if route == "/api/mkdir":
            path = body.get("path")
            if not path:
                return self.send_error_json("缺少 path 参数")
            path = os.path.normpath(path)
            if p.exists(path, bridge):
                return self.send_error_json("目标已存在", 409)
            p.add_mkdir(path)
            return finish({"path": path})

        if route == "/api/rename":
            path, name = body.get("path"), body.get("name")
            if not path or not name or "/" in name:
                return self.send_error_json("参数不合法")
            parent = os.path.dirname(path.rstrip("/")) or "/"
            new = join_unix(parent, name)
            if new == path:
                return finish({"path": new})
            if not p.exists(path, bridge):
                return self.send_error_json("源不存在", 404)
            if p.exists(new, bridge):
                return self.send_error_json("目标已存在", 409)
            meta = None
            kind, v = p.resolve(path)
            if kind == "real":
                try:
                    meta = bridge.stat(v)
                except BridgeError:
                    meta = None
            p.add_rename(path, new, meta)
            return finish({"path": new})

        # copy / move
        paths, target = body.get("paths"), body.get("target")
        if not paths or not target:
            return self.send_error_json("缺少 paths/target 参数")
        target = os.path.normpath(target)
        results = []
        existing = p.dir_names(target, bridge)
        for src in paths:
            if not p.exists(src, bridge):
                return self.send_error_json(f"源不存在: {src}", 404)
            base = os.path.basename(src.rstrip("/"))
            name = unique_name(existing, base,
                               suffix="副本" if route == "/api/copy" else None)
            dst = join_unix(target, name)
            if route == "/api/move" and \
                    os.path.dirname(src.rstrip("/")) == target:
                results.append({"src": src, "skipped": True})
                continue
            try:
                meta = fs_stat(src)
                res = meta.get("resolved") or {}
                data = None
                if not meta.get("is_symlink") and res.get("type") == "file":
                    kind, v = p.resolve(src)
                    if kind == "blob":
                        data = p.blobs[v]
                    else:
                        data = bridge.read(v, 0, int(res.get("size", 0)))
                if route == "/api/copy":
                    p.add_copy(src, dst, meta, data)
                else:
                    p.add_rename(src, dst, meta)
            except (BridgeError, KeyError) as e:
                return self.send_error_json(str(e), 409)
            existing.add(name)
            results.append({"src": src, "dst": dst, "name": name})
        return finish({"target": target, "results": results})

    def _handle_api(self, route, parsed):
        bridge: Bridge = app_state["bridge"]

        if route == "/api/info":
            base = {"ok": True, "version": bridge.version(),
                    "writable": bridge.writable(),
                    "pending": app_state["pending"].info()}
            if not bridge.is_open():
                base["open"] = False
                return self.send_json(base)
            device = bridge.info()["device"]
            exists = device_node_exists(device)
            if not exists and not app_state["pending"].count:
                # 设备已移除且没有待写回内容：自动结束会话
                bridge.close()
                app_state["pending"] = None
                base.update({"open": False, "reason": "device_gone"})
                return self.send_json(base)
            base.update({"open": True, "fs": bridge.info(),
                         "device_exists": exists})
            return self.send_json(base)

        if route == "/api/mounts":
            topo = disk_topology_cached()
            cur = None
            if bridge.is_open():
                cur = bridge.info()["device"]
            mounted, mountable = [], []
            for path, m in topo.items():
                if path == cur:
                    continue
                entry = {"path": path, **m}
                (mounted if m.get("mounted") else mountable).append(entry)
            device_exists = True
            cur_mounted = False
            cur_mp = ""
            if bridge.is_open():
                device = bridge.info()["device"]
                device_exists = device_node_exists(device)
                tm = topo.get(device)
                if tm:
                    cur_mounted = bool(tm.get("mounted"))
                    cur_mp = tm.get("mount_point", "")
            return self.send_json({"ok": True, "mounted": mounted,
                                   "mountable": mountable,
                                   "device_exists": device_exists,
                                   "current_mounted": cur_mounted,
                                   "current_mount_point": cur_mp})

        if route == "/api/flush":
            return self.send_json(do_flush())

        if route == "/api/open":
            body = self.read_body() if self.command == "POST" else {}
            path = body.get("path") or self.q1("path")
            if not path:
                return self.send_error_json("缺少 path 参数")
            path = os.path.normpath(path)
            # 切换前先把当前会话的缓冲写回磁盘
            if bridge.is_open() and app_state["pending"] and \
                    app_state["pending"].count:
                r = do_flush()
                if not r.get("ok"):
                    return self.send_json(
                        {"ok": False,
                         "error": f"切换前写回失败: {r.get('error')}"}, 409)
            is_dev = path.startswith("/dev/")
            # 块设备始终只读访问（避免与宿主系统挂载状态冲突）
            try:
                fs = bridge.open(path, writable=not (body.get("readOnly") or is_dev))
            except BridgeError as e:
                msg = str(e)
                low = msg.lower()
                if is_dev and ("not permitted" in low or "permission denied" in low):
                    return self.send_json({"ok": False, "error": msg,
                                           "need_permission": True,
                                           "device": path})
                if is_dev and ("resource busy" in low or "ebusy" in low
                               or "busy" in low):
                    # 已被系统挂载：按需卸载后重试
                    if body.get("unmount_first"):
                        invalidate_topo_cache()
                        rr = run_diskutil("unmount", path, timeout=60)
                        out = ""
                        if rr is not None:
                            out = ((rr.stdout or b"") + (rr.stderr or b"")) \
                                .decode("utf-8", "replace").strip()
                        if rr is None or rr.returncode != 0:
                            return self.send_json(
                                {"ok": False, "busy": True, "device": path,
                                 "error": f"卸载失败: {out or '系统拒绝卸载'}"},
                                409)
                        invalidate_topo_cache()
                        fs = bridge.open(path, writable=False)   # 仍失败则向上抛
                    else:
                        meta = disk_topology_cached().get(path, {})
                        return self.send_json(
                            {"ok": False, "error": msg, "busy": True,
                             "device": path,
                             "content": meta.get("content", ""),
                             "mount_point": meta.get("mount_point", "")})
                else:
                    raise
            app_state["pending"] = PendingFS(app_state["buffer_limit"])
            return self.send_json({"ok": True, "fs": fs,
                                   "writable": bridge.writable(),
                                   "version": bridge.version()})

        if route == "/api/close":
            body = self.read_body() if self.command == "POST" else {}
            if not body.get("discard"):
                r = do_flush()
                if not r.get("ok"):
                    return self.send_json(
                        {"ok": False,
                         "error": f"写回失败，文件系统保持打开: {r.get('error')}",
                         "flushed": r.get("flushed", 0)}, 409)
                flushed = r.get("flushed", 0)
            else:
                flushed = 0
            bridge.close()
            app_state["pending"] = None
            return self.send_json({"ok": True, "flushed": flushed,
                                   "discarded": bool(body.get("discard"))})

        if route == "/api/list":
            if not bridge.is_open():
                return self.send_error_json("尚未打开文件系统", 409)
            path = self.q1("path") or "/"
            try:
                d = fs_list(path)
            except BridgeError as e:
                return self.send_error_json(str(e), 400)
            return self.send_json({"ok": True, **d})

        if route == "/api/stat":
            if not bridge.is_open():
                return self.send_error_json("尚未打开文件系统", 409)
            path = self.q1("path")
            if not path:
                return self.send_error_json("缺少 path 参数")
            try:
                d = fs_stat(path)
            except BridgeError as e:
                return self.send_error_json(str(e), 404)
            return self.send_json({"ok": True, **d})

        if route in ("/api/file", "/api/download"):
            if not bridge.is_open():
                return self.send_error_json("尚未打开文件系统", 409)
            return self.serve_file_content(download=(route == "/api/download"))

        if route == "/api/probe":
            path = self.q1("path")
            if not path:
                return self.send_error_json("缺少 path 参数")
            return self.send_json({"ok": True, **bridge.probe(path)})

        if route == "/api/scan":
            return self.send_json({"ok": True, **scan_candidates()})

        if route in ("/api/mkdir", "/api/rename", "/api/copy", "/api/move"):
            if not bridge.writable():
                return self.send_error_json("当前文件系统不可写（只读方式打开）", 403)
            return self.handle_write(route)

        if route == "/api/check":
            body = self.read_body() if self.command == "POST" else {}
            return self.send_json({"ok": True,
                                   "job": tool_check_job(bool(body.get("repair")))})

        if route == "/api/format":
            body = self.read_body() if self.command == "POST" else {}
            return self.send_json({"ok": True, "job": format_job(body)})

        if route == "/api/save":
            body = self.read_body() if self.command == "POST" else {}
            return self.send_json({"ok": True, "job": save_job(body)})

        if route == "/api/reveal":
            body = self.read_body() if self.command == "POST" else {}
            path = body.get("path") or self.q1("path")
            if not path or not os.path.exists(path):
                return self.send_error_json("路径不存在")
            opener = "/usr/bin/open" if sys.platform == "darwin" else "xdg-open"
            try:
                subprocess.run([opener, path], check=False, timeout=30)
            except FileNotFoundError:
                return self.send_error_json(f"找不到 {opener}")
            return self.send_json({"ok": True, "path": path})

        if route == "/api/job":
            jid = self.q1("id") or ""
            with JOBS_LOCK:
                job = JOBS.get(jid)
            if not job:
                return self.send_error_json("任务不存在", 404)
            return self.send_json({"ok": True, "job": dict(job)})

        if route == "/api/elevate":
            body = self.read_body() if self.command == "POST" else {}
            devs = body.get("devices") or []
            if not devs:
                return self.send_error_json("缺少 devices 参数")
            bad = [d for d in devs if not DEV_PATH_RE.match(str(d))]
            if bad:
                return self.send_error_json(f"非法设备路径: {bad}")
            cmd = "chmod a+r " + " ".join(devs)
            script = (f'do shell script "{cmd}" with administrator privileges '
                      f'with prompt "Ext 文件系统浏览器请求读取磁盘（仅授予只读权限）"')
            try:
                r = subprocess.run(["/usr/bin/osascript", "-e", script],
                                   capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                return self.send_error_json("授权超时")
            if r.returncode == 0:
                bridge = app_state["bridge"]
                results = []
                for d in devs:
                    try:
                        results.append({"path": d, "ok": True, **bridge.probe(d)})
                    except BridgeError as e:
                        results.append({"path": d, "ok": False, "error": str(e)})
                return self.send_json({"ok": True, "results": results})
            err = (r.stderr or "").strip() or "授权被取消或失败"
            return self.send_json({"ok": False, "error": err}, 403)

        if route in ("/api/mount", "/api/unmount"):
            body = self.read_body() if self.command == "POST" else {}
            device = body.get("device") or ""
            if not DEV_PATH_RE.match(str(device)):
                return self.send_error_json(f"非法设备路径: {device}")
            closed_session = False
            if route == "/api/unmount" and bridge.is_open() and \
                    os.path.normpath(bridge.info()["device"]) == device:
                # 卸载的是当前打开的设备：先写回缓冲并结束会话，再执行系统卸载
                r = do_flush()
                if not r.get("ok"):
                    return self.send_error_json(
                        f"缓冲写回失败，已保持打开: {r.get('error')}", 409)
                bridge.close()
                app_state["pending"] = None
                closed_session = True
            if route == "/api/mount":
                args = ["mount"] + (["-readOnly"] if body.get("readOnly")
                                    else []) + [device]
            else:
                args = ["unmount", device]
            invalidate_topo_cache()
            r = run_diskutil(*args, timeout=60)
            if r is None:
                return self.send_error_json("diskutil 不可用（仅支持 macOS）")
            out = ((r.stdout or b"") + (r.stderr or b"")).decode(
                "utf-8", "replace").strip()
            ok = r.returncode == 0
            mount_point = ""
            if ok:
                invalidate_topo_cache()
                topo = disk_topology_cached()
                mount_point = topo.get(device, {}).get("mount_point", "")
            return self.send_json({"ok": ok, "output": out,
                                   "mount_point": mount_point,
                                   "closed_session": closed_session})

        if route == "/api/open-with":
            path = self.q1("path") or (self.read_body() or {}).get("path")
            if not path:
                return self.send_error_json("缺少 path 参数")
            return self.send_json(open_with_system(bridge, path))

        return self.send_error_json(f"未知 API: {route}", 404)

    # -- 文件内容（预览 / 下载，支持 Range） ------------------------------------
    def serve_file_content(self, download: bool):
        bridge: Bridge = app_state["bridge"]
        p: PendingFS = app_state["pending"]
        path = self.q1("path")
        if not path:
            return self.send_error_json("缺少 path 参数")
        try:
            st = fs_stat(path)
        except BridgeError as e:
            return self.send_error_json(str(e), 404)
        # 缓冲中的符号链接按目标读取（最多跟一层）
        if st.get("is_symlink") and st.get("symlink") and p is not None:
            try:
                path = st["symlink"]
                st = fs_stat(path)
            except BridgeError:
                pass
        res = st.get("resolved") or st.get("raw") or {}
        size = int(res.get("size", 0))
        ftype = res.get("type")
        if ftype == "dir":
            return self.send_error_json("这是一个目录", 400)
        if ftype not in ("file", "symlink", "unknown"):
            return self.send_error_json(f"不支持预览该类型: {ftype}", 400)

        # 解析读取来源：内存 blob 或磁盘真实路径
        blob = None
        real_path = path
        if p is not None:
            kind, v = p.read_bytes(path)
            if kind == "missing":
                return self.send_error_json("路径不存在", 404)
            if kind == "blob":
                blob = v
            else:
                real_path = v

        cap = self.q1("max")
        limit = int(cap) if cap and cap.isdigit() else None
        length = min(size, limit) if limit else size

        fname = os.path.basename(path.rstrip("/")) or "download.bin"

        rng = self.headers.get("Range")
        start, end = 0, length - 1
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else length - 1
                else:  # suffix range: 最后 N 字节
                    start = max(0, length - int(m.group(2)))
                    end = length - 1
                if start >= length or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{length}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        out_len = end - start + 1 if length > 0 else 0
        self.send_response(status)
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(out_len))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quote(fname)}")
        if status == 206:
            self.send_header(
                "Content-Range", f"bytes {start}-{end}/{length}")
        self.end_headers()
        if self.command == "HEAD" or out_len == 0:
            return

        offset = start
        remaining = out_len
        while remaining > 0:
            if blob is not None:
                chunk = blob[offset:offset + min(CHUNK, remaining)]
                if not chunk:
                    break
                self.wfile.write(chunk)
                offset += len(chunk)
                remaining -= len(chunk)
                continue
            n = min(CHUNK, remaining)
            data = bridge.read(real_path, offset, n)
            if not data:
                break
            self.wfile.write(data)
            offset += len(data)
            remaining -= len(data)


# --------------------------------------------------------------------------- #
# 静态文件
# --------------------------------------------------------------------------- #
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def serve_static(self: Handler, name: str):
    frontend: Path = app_state["frontend"]
    name = os.path.normpath(name).lstrip("/")
    if name.startswith(".."):
        return self.send_error_json("非法路径", 400)
    fp = frontend / name
    if fp.is_dir():
        fp = fp / "index.html"
    if not fp.is_file():
        return self.send_error_json(f"未找到: {name}", 404)
    data = fp.read_bytes()
    ext = fp.suffix.lower()
    self.send_response(200)
    self.send_header("Content-Type",
                     STATIC_TYPES.get(ext, "application/octet-stream"))
    self.send_header("Content-Length", str(len(data)))
    self.send_header("Cache-Control", "no-cache")
    self.end_headers()
    if self.command != "HEAD":
        self.wfile.write(data)


Handler.serve_static = serve_static


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
app_state = {"bridge": None, "frontend": DEFAULT_FRONTEND,
             "pending": None, "buffer_limit": 256 * 1024 * 1024}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import traceback
        sys.stderr.write(f"[conn-error] {client_address}\n")
        traceback.print_exc(file=sys.stderr)


def pick_port(preferred: int, bind: str) -> int:
    import socket
    for port in range(preferred, preferred + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((bind, port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port")


def main():
    ap = argparse.ArgumentParser(description="Ext 文件系统浏览器后端")
    ap.add_argument("--bridge", default=str(DEFAULT_BRIDGE),
                    help="libe2fsbridge.dylib 路径")
    ap.add_argument("--frontend", default=str(DEFAULT_FRONTEND),
                    help="前端静态文件目录")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0,
                    help="监听端口（0 = 从 8765 起自动选择）")
    ap.add_argument("--open", dest="open_path", default=None,
                    help="启动时自动打开的设备/镜像路径")
    ap.add_argument("--buffer-limit", type=int, default=256,
                    help="写缓冲内存上限（MB），达到后自动写回磁盘（默认 256）")
    ap.add_argument("--e2fsprogs", default=os.environ.get(
                        "E2FSPROGS_BIN",
                        str(Path.home() / "e2fsprogs")),
                    help="e2fsprogs 源码树/安装位置（定位 e2fsck、mke2fs）")
    args = ap.parse_args()

    if not os.path.exists(args.bridge):
        sys.exit(f"桥接库不存在: {args.bridge}（先运行 make 构建）")
    app_state["bridge"] = Bridge(args.bridge)
    app_state["frontend"] = Path(args.frontend).resolve()
    app_state["buffer_limit"] = max(1, args.buffer_limit) * 1024 * 1024
    os.environ["E2FSPROGS_BIN"] = args.e2fsprogs
    app_state["tools"] = {"e2fsck": find_tool("e2fsck"),
                          "mke2fs": find_tool("mke2fs")}

    port = args.port or pick_port(8765, args.bind)
    httpd = Server((args.bind, port), Handler)

    if args.open_path:
        try:
            fs = app_state["bridge"].open(args.open_path)
            app_state["pending"] = PendingFS(app_state["buffer_limit"])
            print(f"[open] {args.open_path} -> "
                  f"{fs.get('fstype')} {fs.get('label')} "
                  f"writable={app_state['bridge'].writable()}", file=sys.stderr)
        except BridgeError as e:
            print(f"[open] failed: {e}", file=sys.stderr)

    # 机器可读输出，供 run.sh / webview 获取实际端口
    print(f"E2FS_BROWSER_PORT={port}")
    print(f"E2FS 浏览器后端已启动: http://{args.bind}:{port}/  "
          f"(libext2fs {app_state['bridge'].version()})")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
