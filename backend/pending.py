#!/usr/bin/env python3
"""
pending.py — 写操作缓冲区（write-back buffer）。

所有修改（mkdir / rename / copy）先记入内存：
  - ops          有序操作日志（写回时按原顺序重放）
  - blobs        复制产生的文件数据（整份缓冲在内存，供预览/下载读取）
  - entries      虚拟条目元数据（新建/移动到缓冲区的路径）
  - prefix_maps  目录级路径映射（虚拟目录 -> 磁盘上的真实源目录）
  - removed      被移走后从视图里隐藏的虚拟路径

浏览（list/stat/read）一律先经过 overlay：缓冲区里有就展示内存版本，
否则落到磁盘真实路径。写回 = 按序重放操作日志，全部完成后清空缓冲。
"""

import time


def _now():
    return int(time.time())


class PendingFS:
    def __init__(self, limit_bytes: int):
        self.limit = limit_bytes
        self.reset()

    def reset(self):
        self.ops = []
        self.blobs = {}
        self.entries = {}
        self.prefix_maps = {}
        self.removed = set()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def count(self) -> int:
        return len(self.ops)

    def buffered_bytes(self) -> int:
        return sum(len(b) for b in self.blobs.values())

    def info(self) -> dict:
        return {"count": self.count, "bytes": self.buffered_bytes(),
                "limit": self.limit}

    # ------------------------------------------------------------------ #
    # 路径工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def parent(path: str) -> str:
        p = path.rstrip("/")
        i = p.rfind("/")
        return "/" if i <= 0 else p[:i]

    @staticmethod
    def name(path: str) -> str:
        p = path.rstrip("/")
        return p[p.rfind("/") + 1:] or "/"

    def _hidden(self, path: str) -> bool:
        return any(path == r or path.startswith(r + "/") for r in self.removed)

    def _longest_map(self, path: str):
        """最长前缀的目录映射；返回映射后的真实路径或 None"""
        best_vp, best_rp = None, None
        for vp, rp in self.prefix_maps.items():
            if path == vp or path.startswith(vp + "/"):
                if best_vp is None or len(vp) > len(best_vp):
                    best_vp, best_rp = vp, rp
        if best_vp is None:
            return None
        return best_rp + path[len(best_vp):]

    def resolve(self, path: str):
        """把缓冲视图中的路径解析为落点。

        返回 (kind, value)：
          missing  —— 路径已被移走/不存在于缓冲视图
          blob     —— value=路径，文件数据在内存
          entry    —— value=虚拟条目元数据（新建目录/符号链接等）
          real     —— value=磁盘上的真实路径（可能经过映射）
        """
        if self._hidden(path):
            return ("missing", None)
        if path in self.blobs:
            return ("blob", path)
        e = self.entries.get(path)
        if e:
            if e.get("backing"):
                return ("real", e["backing"])
            return ("entry", e)
        mapped = self._longest_map(path)
        if mapped:
            return ("real", mapped)
        return ("real", path)

    def exists(self, path: str, bridge) -> bool:
        kind, v = self.resolve(path)
        if kind == "missing":
            return False
        if kind in ("blob", "entry"):
            return True
        try:
            bridge.stat(v)
            return True
        except Exception:
            return False

    def dir_names(self, path: str, bridge) -> set:
        """缓冲视图中 path 目录下已占用的名字（真实 + 虚拟）"""
        names = set()
        kind, v = self.resolve(path)
        if kind == "real":
            try:
                names |= {e["name"] for e in bridge.list(v)["entries"]}
            except Exception:
                pass
        for op in self.ops:
            if op["op"] == "mkdir":
                if self.parent(op["path"]) == path:
                    names.add(self.name(op["path"]))
            elif op["op"] in ("copy", "rename"):
                if self.parent(op["new"]) == path:
                    names.add(self.name(op["new"]))
        return names

    # ------------------------------------------------------------------ #
    # 记录操作（只改内存，不碰磁盘）
    # ------------------------------------------------------------------ #
    def add_mkdir(self, path: str):
        self.ops.append({"op": "mkdir", "path": path})
        self.entries[path] = {"type": "dir", "size": 4096, "mode": 0o755,
                              "perms": "drwxr-xr-x", "mtime": _now(),
                              "ino": 0, "nlink": 2}

    def add_rename(self, old: str, new: str, meta: dict | None = None):
        """meta：old 为磁盘真实路径时调用方提供的 stat 结果（判断目录/文件用）"""
        kind, v = self.resolve(old)
        if kind == "blob":
            self.blobs[new] = self.blobs.pop(old)
        elif kind == "entry":
            self._move_tree(old, new)
        else:  # real
            r = (meta or {}).get("raw") or {}
            is_dir = (meta or {}).get("resolved", {}).get("type") == "dir" \
                if not (meta or {}).get("is_symlink") else False
            entry = {"type": "symlink" if (meta or {}).get("is_symlink")
                     else r.get("type", "unknown"),
                     "size": r.get("size", 0), "mode": r.get("mode", 0o644),
                     "perms": r.get("perms", ""), "mtime": r.get("mtime", _now()),
                     "ino": r.get("ino", 0), "nlink": r.get("nlink", 1),
                     "backing": v}
            if (meta or {}).get("is_symlink"):
                entry["target"] = (meta or {}).get("symlink")
            self.entries[new] = entry
            if is_dir:
                self.prefix_maps[new] = v
        self.removed.add(old)
        self.ops.append({"op": "rename", "old": old, "new": new})

    def add_delete(self, path: str):
        """删除进缓冲：视图立即隐藏该子树，写回时才真正从磁盘删除"""
        self.ops.append({"op": "delete", "path": path})
        self.removed.add(path)
        for k in [k for k in self.entries
                  if k == path or k.startswith(path + "/")]:
            del self.entries[k]
        for k in [k for k in self.blobs
                  if k == path or k.startswith(path + "/")]:
            del self.blobs[k]
        for k in [k for k in self.prefix_maps
                  if k == path or k.startswith(path + "/")]:
            del self.prefix_maps[k]

    def add_copy(self, old: str, new: str, source_meta: dict, data: bytes | None):
        """data 非 None 表示常规文件内容已缓冲进内存；目录/符号链接只记录元数据"""
        kind, v = self.resolve(old)
        r = source_meta.get("raw") or {}
        is_symlink = source_meta.get("is_symlink")
        rtype = "symlink" if is_symlink else r.get("type", "file")

        if data is not None:
            self.blobs[new] = data
            self.entries[new] = {"type": "file", "size": len(data),
                                 "mode": r.get("mode", 0o644),
                                 "perms": r.get("perms", "-rw-r--r--"),
                                 "mtime": _now(), "ino": 0, "nlink": 1}
        elif is_symlink:
            self.entries[new] = {"type": "symlink", "size": r.get("size", 0),
                                 "mode": r.get("mode", 0o777),
                                 "perms": r.get("perms", "lrwxrwxrwx"),
                                 "mtime": _now(), "ino": 0, "nlink": 1,
                                 "target": source_meta.get("symlink")}
        elif rtype == "dir":
            # 目录复制：整棵子树通过前缀映射透明指向真实源目录
            self.entries[new] = {"type": "dir", "size": r.get("size", 4096),
                                 "mode": r.get("mode", 0o755),
                                 "perms": r.get("perms", "drwxr-xr-x"),
                                 "mtime": _now(), "ino": 0, "nlink": 2}
            if kind == "real":
                self.prefix_maps[new] = v
            # 虚拟空目录的复制：无映射（空目录）
        else:
            raise ValueError(f"不支持的缓冲复制类型: {rtype}")
        self.ops.append({"op": "copy", "old": old, "new": new})

    def _move_tree(self, old: str, new: str):
        for k in [k for k in self.entries
                  if k == old or k.startswith(old + "/")]:
            self.entries[new + k[len(old):]] = self.entries.pop(k)
        for k in [k for k in self.blobs
                  if k == old or k.startswith(old + "/")]:
            self.blobs[new + k[len(old):]] = self.blobs.pop(k)
        for k in [k for k in self.prefix_maps
                  if k == old or k.startswith(old + "/")]:
            self.prefix_maps[new + k[len(old):]] = self.prefix_maps.pop(k)

    # ------------------------------------------------------------------ #
    # 视图：list / stat
    # ------------------------------------------------------------------ #
    def list_dir(self, path: str, bridge) -> list:
        kind, v = self.resolve(path)
        if kind == "missing":
            raise KeyError(path)

        real = None
        if path in self.prefix_maps:
            real = bridge.list(self.prefix_maps[path])["entries"]
        elif kind == "real":
            real = bridge.list(v)["entries"]      # 非目录时由桥接层抛错
        elif kind == "entry" and self.entries[path].get("type") == "dir":
            real = []
        else:
            raise KeyError(path)                   # 对文件列目录

        names = {e["name"]: dict(e) for e in real or []}

        for op in self.ops:
            if op["op"] == "rename":
                if self.parent(op["old"]) == path:
                    names.pop(self.name(op["old"]), None)
                if self.parent(op["new"]) == path:
                    nm = self.name(op["new"])
                    names[nm] = self._synth_list_entry(op["old"], nm, bridge)
            elif op["op"] == "copy" and self.parent(op["new"]) == path:
                nm = self.name(op["new"])
                names[nm] = self._synth_list_entry(op["new"], nm, bridge)
            elif op["op"] == "delete" and self.parent(op["path"]) == path:
                names.pop(self.name(op["path"]), None)
            elif op["op"] == "mkdir" and self.parent(op["path"]) == path:
                nm = self.name(op["path"])
                e = dict(self.entries.get(op["path"]) or {})
                e.update({"name": nm, "pending": 1})
                names[nm] = e
        return list(names.values())

    def _synth_list_entry(self, src_path: str, name: str, bridge) -> dict:
        kind, v = self.resolve(src_path)
        base = {"name": name, "ino": 0, "pending": 1}
        if kind == "blob":
            data = self.blobs[v]
            base.update({"type": "file", "size": len(data), "mode": 0o644,
                         "perms": "-rw-r--r--", "mtime": _now(), "nlink": 1})
            return base
        if kind == "entry":
            e = self.entries[v]
            base.update({"type": e.get("type", "unknown"),
                         "size": e.get("size", 0), "mode": e.get("mode", 0),
                         "perms": e.get("perms", ""), "mtime": e.get("mtime", 0),
                         "nlink": e.get("nlink", 1),
                         "target": e.get("target")})
            return base
        try:
            st = bridge.stat(v)
        except Exception:
            return base
        r = st.get("raw") or {}
        base.update({"type": "symlink" if st.get("is_symlink")
                     else r.get("type", "unknown"),
                     "size": r.get("size", 0), "mode": r.get("mode", 0),
                     "perms": r.get("perms", ""), "mtime": r.get("mtime", 0),
                     "nlink": r.get("nlink", 1), "ino": r.get("ino", 0),
                     "target": st.get("symlink")})
        return base

    def stat(self, path: str, bridge) -> dict:
        kind, v = self.resolve(path)
        if kind == "missing":
            raise KeyError(path)

        if kind == "blob":
            data = self.blobs[v]
            res = {"type": "file", "size": len(data), "mode": 0o644,
                   "perms": "-rw-r--r--", "nlink": 1, "ino": 0,
                   "uid": 0, "gid": 0, "atime": _now(), "mtime": _now(),
                   "ctime": _now(), "blocks_512": (len(data) + 511) // 512}
            return {"path": path, "is_symlink": False, "symlink": None,
                    "resolved": res, "raw": res, "pending": True}

        if kind == "entry":
            e = self.entries[v]
            is_lnk = e.get("type") == "symlink"
            res = {"type": e.get("type", "unknown"), "size": e.get("size", 0),
                   "mode": e.get("mode", 0), "perms": e.get("perms", ""),
                   "nlink": e.get("nlink", 1), "ino": 0, "uid": 0, "gid": 0,
                   "atime": e.get("mtime", _now()), "mtime": e.get("mtime", _now()),
                   "ctime": e.get("mtime", _now()), "blocks_512": 0}
            return {"path": path,
                    "is_symlink": is_lnk,
                    "symlink": e.get("target") if is_lnk else None,
                    "resolved": None if (is_lnk and not e.get("target")) else res,
                    "raw": res, "pending": True}

        st = bridge.stat(v)
        st["path"] = path       # 对外呈现缓冲视图中的路径
        return st

    def read_bytes(self, path: str):
        """供读接口判断：返回 ("blob", bytes) 或 ("real", 真实路径) 或 ("missing", None)"""
        kind, v = self.resolve(path)
        if kind == "missing":
            return ("missing", None)
        if kind == "blob":
            return ("blob", self.blobs[v])
        return ("real", v)


class PendingNone:
    """未打开文件系统时的空实现"""
    count = 0

    def info(self):
        return {"count": 0, "bytes": 0, "limit": 0}
