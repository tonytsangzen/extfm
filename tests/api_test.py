#!/usr/bin/env python3
"""api_test.py — 全功能端到端 API 测试。

用法: python3 tests/api_test.py [base_url]
前置: 服务器已打开 images/ext4.img（可写会话）。测试在缓冲内进行，
     每个用例组自行 flush 并用 debugfs 验证落盘效果。
"""
import json
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse

B = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
DEBUGFS = "/Users/tony/work/e2fsprogs/debugfs/debugfs"
IMG = "images/ext4.img"

passed = failed = 0


def check(name, cond):
    global passed, failed
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if cond:
        passed += 1
    else:
        failed += 1


def req(route, body=None, raw=False):
    route = urllib.parse.quote(route, safe="/?&=%")
    r = urllib.request.Request(B + route)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        r.add_header("Content-Type", "application/json")
        r.get_method = lambda: "POST"
    try:
        with urllib.request.urlopen(r, data) as resp:
            payload = resp.read()
            code = resp.status
            hdrs = dict(resp.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        code = e.code
        hdrs = dict(e.headers)
    if raw:
        return code, payload, hdrs
    try:
        return code, json.loads(payload)
    except ValueError:
        return code, {"_raw": payload.decode("utf-8", "replace")}


def disk_names(path_in_img):
    out = subprocess.run([DEBUGFS, "-R", f"ls {path_in_img}", IMG],
                         capture_output=True, text=True).stdout
    import re
    names = []
    for tok in out.split():
        if re.fullmatch(r"[\x20-\x7e]+", tok) and not tok.startswith("(") \
                and not tok.rstrip("(0123456789)").isdigit():
            names.append(tok)
    return names


def wait_job(jid, timeout=60):
    import time
    for _ in range(int(timeout * 2)):
        code, d = req(f"/api/job?id={jid}")
        if d["job"]["status"] != "running":
            return d["job"]
        time.sleep(0.5)
    return {"status": "timeout"}


# ---------------------------------------------------------------- #
print("=== 会话与信息 ===")
subprocess.run(["bash", "tests/make_images.sh"], capture_output=True)
code, d = req("/api/open", {"path": IMG})
check("打开测试镜像", d["ok"])
code, d = req("/api/info")
check("info ok", code == 200 and d["ok"] and d["open"])
check("info writable", d.get("writable") is True)
check("info pending 结构", isinstance(d.get("pending"), dict))

print("=== 列表 / stat ===")
code, d = req("/api/list?path=/")
check("list /", d["ok"] and d["count"] == 7)
names = {e["name"] for e in d["entries"]}
check("根目录包含 docs/big.bin/软链接.txt",
      {"docs", "big.bin", "软链接.txt"} <= names)
code, d = req("/api/list?path=/nope")
check("list 不存在目录报错", not d["ok"])
code, d = req("/api/stat?path=/软链接.txt")
check("stat 符号链接带目标", d["ok"] and d["is_symlink"]
      and d["symlink"] == "/docs/说明.txt" and d["resolved"]["type"] == "file")
code, d = req("/api/stat?path=/missing")
check("stat 不存在 → 404", code == 404)

print("=== 文件读取（含 Range）===")
code, raw, hdrs = req("/api/file?path=/docs/说明.txt", raw=True)
check("读取文本", b"quick brown fox" in raw)
code, raw, hdrs = req("/api/file?path=/big.bin", raw=True)
check("读取 big.bin 全量", len(raw) == 4194304)
code, raw, hdrs = req("/api/file?path=/big.bin", raw=True)  # Range 单独测
r2 = urllib.request.Request(B + "/api/file?path=%2Fbig.bin")
r2.add_header("Range", "bytes=100-131")
with urllib.request.urlopen(r2) as resp:
    part = resp.read()
    check("Range 206", resp.status == 206 and len(part) == 32)
code, raw, hdrs = req("/api/file?path=/docs/说明.txt&max=16", raw=True)
check("max 截断", len(raw) == 16)

print("=== 缓冲写操作：mkdir/rename/copy（视图即时、磁盘不动）===")
code, d = req("/api/mkdir", {"path": "/apiDir"})
check("mkdir 缓冲", d["ok"] and d["pending"]["count"] >= 1)
code, d = req("/api/rename", {"path": "/docs", "name": "docs2"})
check("rename 缓冲", d["ok"])
code, d = req("/api/list?path=/")
view = {e["name"] for e in d["entries"]}
check("视图反映缓冲：docs2 出现、docs 消失", "docs2" in view and "docs" not in view)
code, d = req("/api/copy", {"paths": ["/docs2/说明.txt"], "target": "/apiDir"})
check("copy 到缓冲目录", d["ok"] and len(d["results"]) == 1)
code, d = req("/api/list?path=/apiDir")
check("缓冲目录视图含说明.txt", "说明.txt" in
      {e["name"] for e in d["entries"]})
code, raw, hdrs = req("/api/file?path=/apiDir/说明.txt", raw=True)
check("缓冲文件可读（内存/映射）", b"quick brown fox" in raw)
code, d = req("/api/rename", {"path": "/docs2/说明.txt", "name": "截图.png"})
check("rename 目标冲突 409", code == 409)
code, d = req("/api/delete", {"paths": ["/不存在的名字"]})
check("delete 不存在 404", code == 404)

print("=== 写回：磁盘落盘 + 一致性 ===")
code, d = req("/api/flush")
check("flush ok", d["ok"] and d["flushed"] >= 3)
code, d = req("/api/info")
check("flush 后 pending 清零", d["pending"]["count"] == 0)
names = disk_names("/")
check("磁盘出现 docs2 与 apiDir",
      "docs2" in names and "apiDir" in names)
r = subprocess.run(["/Users/tony/work/e2fsprogs/e2fsck/e2fsck", "-fn", IMG],
                   capture_output=True, text=True)
check("e2fsck 干净", r.returncode == 0)

print("=== 删除（缓冲 → 视图隐藏 → flush 落盘消失）===")
code, d = req("/api/delete", {"paths": ["/apiDir/说明.txt"]})
check("delete 缓冲", d["ok"] and d["pending"]["count"] >= 1)
code, d = req("/api/list?path=/apiDir")
check("删除后视图隐藏", "说明.txt" not in
      {e["name"] for e in d["entries"]})
code, d = req("/api/delete", {"paths": ["/apiDir"]})
check("delete 目录（此时已空）", d["ok"])
code, d = req("/api/flush")
check("delete flush", d["ok"])
r = subprocess.run(["/Users/tony/work/e2fsprogs/e2fsck/e2fsck", "-fn", IMG],
                   capture_output=True, text=True)
check("删除后 e2fsck 干净", r.returncode == 0)

print("=== 保存：克隆 / 另存为副本 ===")
subprocess.run(["rm", "-f", "/tmp/t_clone.img", "/tmp/t_copy.img"])
code, d = req("/api/save", {"target": "/tmp/t_clone.img", "mode": "clone",
                            "force": True})
job = wait_job(d["job"]["id"])
check("clone 完成", job["status"] == "done")
r = subprocess.run(["cmp", IMG, "/tmp/t_clone.img"], capture_output=True)
check("克隆与源一致", r.returncode == 0)
# 制造缓冲 → 另存为副本：副本含改动、原盘不动
code, d = req("/api/mkdir", {"path": "/另存标记"})
code, d = req("/api/save", {"target": "/tmp/t_copy.img", "mode": "saveas",
                            "force": True})
job = wait_job(d["job"]["id"])
check("saveas 完成", job["status"] == "done")
code, d = req("/api/open", {"path": "/tmp/t_copy.img"})
check("打开副本", d["ok"] and d["writable"])
code, d = req("/api/list?path=/")
check("副本含缓冲改动（另存标记）", "另存标记" in
      {e["name"] for e in d["entries"]})
code, d = req("/api/close")
check("关闭副本", d["ok"])
orig_names = disk_names("/")
check("原盘不含缓冲改动", not any("另存" in n for n in orig_names))

print("=== 检查 / 格式化 ===")
code, d = req("/api/open", {"path": IMG})
check("重新打开镜像", d["ok"])
code, d = req("/api/check", {"repair": False})
if "job" not in d:
    print("   check 启动失败:", d)
    job = {"status": "error", "returncode": -1, "output": ""}
else:
    job = wait_job(d["job"]["id"])
if job["returncode"] not in (0,):
    print("   check 输出:\n" + job["output"][-600:])
check("check 只读干净", job["status"] == "done" and job["returncode"] == 0)
code, d = req("/api/format",
              {"target": "/tmp/t_fmt.img", "new_file": True, "size_mb": 8,
               "type": "ext4", "block_size": 1024, "label": "APITEST",
               "features": ["has_journal", "^64bit"], "force": True})
job = wait_job(d["job"]["id"])
check("format 完成", job["status"] == "done")
code, d = req("/api/open", {"path": "/tmp/t_fmt.img"})
fs = d.get("fs", {})
check("格式化参数生效（ext4/1KB/标签）",
      d["ok"] and fs.get("fstype") == "ext4" and fs.get("label") == "APITEST"
      and fs.get("block_size") == 1024)
code, d = req("/api/format", {"target": "/tmp/t_fmt.img", "type": "ext2",
                              "force": True})
job = wait_job(d["job"]["id"])
check("格式化当前设备后自动重开", job["status"] == "done"
      and "已重新打开" in job["output"])
code, d = req("/api/info")
check("重开后为 ext2", d["fs"]["fstype"] == "ext2")
subprocess.run(["rm", "-f", "/tmp/t_fmt.img", "/tmp/t_clone.img",
                "/tmp/t_copy.img"])

print("=== 只读会话门控 ===")
code, d = req("/api/open", {"path": IMG, "readOnly": True})
check("只读打开 writable=false", d.get("writable") is False)
code, d = req("/api/mkdir", {"path": "/ro-test"})
check("只读会话写操作 403", code == 403)
code, d = req("/api/open", {"path": IMG})

print("=== 挂载状态端点 ===")
code, d = req("/api/mounts")
check("mounts 结构", d["ok"] and isinstance(d["mounted"], list)
      and isinstance(d["mountable"], list) and "device_exists" in d)
code, d = req("/api/elevate", {"devices": ["/etc/hosts"]})
check("elevate 非法设备 400", code == 400)

print("=== 收尾 ===")
req("/api/close")
code, d = req("/api/info")
if "open" not in d:
    print("   info 响应:", d)
check("关闭后 open=false", d.get("open") is False)
code, d = req("/api/list?path=/")
check("未打开时 list 409", code == 409)

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
