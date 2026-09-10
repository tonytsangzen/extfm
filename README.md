# Ext 文件系统浏览器（e2fs）

[![CI](https://github.com/tonytsangzen/extfm/actions/workflows/ci.yml/badge.svg)](https://github.com/tonytsangzen/extfm/actions/workflows/ci.yml)

基于 **e2fsprogs libext2fs** 的 ext2/ext3/ext4 图形浏览器。原生窗口内嵌 WKWebView，
前端模拟文件管理器（Finder 风格），后端通过 C 桥接库直接读取/操作文件系统结构。

支持打开 **块设备**（`/dev/diskNsM` 等）与 **raw 磁盘镜像文件**（`.img`/`.raw`/`.dd`…），
默认可写，可显式只读打开。

```
┌────────────────────────────────────────────────────────┐
│  原生窗口 (Swift + WKWebView)                           │
│  ┌──────────────────────────────────────────────────┐  │
│  │  前端 frontend/ （HTML/CSS/JS 文件管理器）         │  │
│  └──────────────────────── HTTP/JSON ───────────────┘  │
│  ┌──────────────────────────────────────────────────┐  │
│  │  后端 backend/server.py （REST API，仅 127.0.0.1）│  │
│  └───────────────────── ctypes ─────────────────────┘  │
│  ┌──────────────────────────────────────────────────┐  │
│  │  src/bridge.c → libext2fs / libe2p / libuuid     │  │
│  └────────────────────── 只读 I/O ───────────────────┘  │
│              块设备 /dev/…   或   镜像文件 *.img         │
└────────────────────────────────────────────────────────┘
```

## 快速开始

```bash
git clone git@github.com:tonytsangzen/extfm.git
cd extfm

# 0) 准备 e2fsprogs 构建树（提供 libext2fs 静态库与 mke2fs/debugfs/e2fsck）
git clone --depth 1 --branch v1.47.3 https://github.com/tytso/e2fsprogs.git ~/e2fsprogs
cd ~/e2fsprogs && ./configure && make -j && cd -

# 1) 构建桥接库、测试程序与原生窗口（默认在 ~/e2fsprogs，可用 E2FS= 覆盖）
make all
# make all E2FS=/path/to/e2fsprogs

# 2) 制作测试镜像（ext2/ext3/ext4 + 一个非 ext 文件，可选）
make images

# 3) 一键启动（后端 + 原生 WebView 窗口），可直接给一个设备/镜像路径
./run.sh images/ext4.img
./run.sh /dev/disk4s2          # 真实块设备（见下方权限说明）

# 也可以不启原生窗口，用浏览器访问
./run.sh --web images/ext4.img
```

其它命令：

| 命令 | 说明 |
|---|---|
| `make test` | 桥接层只读冒烟测试（open/list/stat/read/probe） |
| `make test-write` | 写操作测试 + e2fsck 一致性校验（在临时镜像上执行） |
| `make images` | 重新生成 `images/` 下的测试镜像 |
| `make run` | 以 `--web` 模式启动 |
| `make clean` | 清理 `build/` |

CI（GitHub Actions，macOS runner）自动完成：构建 e2fsprogs 1.47.3 → 全量构建 →
只读/写回测试（含 e2fsck 一致性校验）→ 后端 REST 冒烟；打 `v*` tag 时自动创建 Release。

`backend/server.py` 可独立配置：`--port`（0=自动从 8765 起）、`--bind`、`--bridge`、`--frontend`、`--open`。

## 功能

- **打开目标**：启动页输入路径（可勾选“只读打开”），页面加载时**自动扫描**：
  - 可移动 / 外置设备优先展示，带“可移动”“已挂载 · 挂载点”等徽标；
  - 每个分区提供**挂载选项**：挂载 / 只读挂载 / 卸载（调用 `diskutil`，逐项显示结果）；
  - 无法读取的块设备显示“需要授权”横幅，一键弹出系统密码框完成**自动授权**（见下方权限说明）；
  - 同时扫描常见目录（桌面/下载/文档/图片/`images/`）下的磁盘镜像，自动探测 ext2/ext3/ext4 卷标、UUID、容量。
- **文件管理器**：面包屑导航（可点击/可编辑）、后退/前进/上级/刷新、列表与网格两种视图、
  点击表头排序（文件夹始终靠前）、当前目录搜索、侧栏设备卡片（容量条、inode、UUID、状态、可写/只读）。
- **打开**：双击进入文件夹 / 预览文件（文本 UTF-8、图片 PNG/JPEG/GIF/WEBP/BMP、音视频走 HTTP Range 流式播放、
  二进制显示十六进制）；右键“用系统应用打开”会把文件提取到临时目录并交给 macOS 默认应用。
- **拖放**：像真实文件管理器一样把文件/文件夹拖到目标文件夹、面包屑路径段或侧栏位置上完成**移动**，
  按住 ⌥（Option）拖放则为**复制**；支持一次拖动整个多选集。
- **复制 / 剪切 / 粘贴**：右键菜单或 `⌘C` / `⌘X` / `⌘V`；复制粘贴自动命名为“xxx 副本”“xxx 副本 2”，
  移动冲突时命名为“xxx 2”；剪切中的项目半透明显示；目录粘贴为完整递归复制（保留权限与稀疏空洞）。
- **重命名 / 新建文件夹 / 删除**：右键或 `F2` 原位内联编辑（自动选中主名，Enter 确认、Esc 取消）；
  新建文件夹后直接进入重命名；删除（`⌘⌫`）支持多选与递归目录，带不可恢复确认——
  与其他修改一样先缓冲，写回磁盘时经 e2fsck 一致性校验。
- **其它**：右键菜单（打开/预览/下载/复制路径/显示简介）、属性面板（inode、权限、时间戳、链接目标与有效性）、
  符号链接跟随与悬空链接提示、硬链接识别（同 inode）、快捷键 `⌘←/⌘→/⌘↑`（后退/前进/上级）、
  `⌘F`（搜索）、`Enter`（打开选中）、`Backspace`（上级）、`Esc`（关闭弹窗）。
- **写缓冲（write-back buffer）**：所有修改（改名/移动/复制/新建文件夹）先缓冲在内存，
  磁盘保持不动；缓冲期间浏览、预览、下载看到的都是缓冲后的最新视图，复制进来的文件数据
  也整份保存在内存中。两种时机写回磁盘：**内存达到上限**（`--buffer-limit`，默认 256 MB，
  超限自动写回并提示）或**点击卸载**（工具栏“写回磁盘”按钮也可随时手动写回）。
  写回 = 按原顺序重放操作日志，重放后自动保持文件系统一致性。
- **维护任务**（工具栏按钮，任务后台执行，输出实时滚动显示）：
  - **文件系统检查**：只读检查（`e2fsck -fn`）或检查并自动修复（`-y`，期间自动关闭并重新打开文件系统）；
  - **格式化**：目标可以是当前设备/镜像，也可以勾选“新建空镜像”并指定大小；可调参数包括
    类型（ext2/ext3/ext4）、块大小、卷标、UUID、inode 大小、保留块百分比与全部常见 `-O` 特性开关；
  - **保存到其他磁盘 / 文件**：另存为副本（连同缓冲改动写入目标，原盘与当前会话保持不变）
    或完整克隆（先写回再做逐字节克隆）；支持写入磁盘设备，传输带实时进度条。
- **安全**：整条链路基于 libext2fs 受控读写；HTTP 仅绑定 127.0.0.1。打开时优先尝试读写
  （需宿主对该设备/镜像有写权限并加载位图成功），失败自动回退只读并在界面标注；
  文件系统状态异常（未正常卸载）时拒绝一切写操作。

## 真实块设备的权限说明（macOS）

真实磁盘的 `/dev/disk*` 节点默认归 `root:operator`，普通用户无法直接读取。本工具的处理方式：

- **打开块设备遇到权限错误时**，界面会弹出确认框，点击“授权并打开”后通过
  `osascript … with administrator privileges` 弹出系统密码框，仅对目标设备执行
  `chmod a+r`（只读授权，不改数据）；设备重新插拔后权限重置，需重新授权。
  也可以在授权横幅中一次勾选全部被拒设备、一条密码完成授权。
- **块设备始终以只读方式访问**（避免与 macOS 的挂载状态冲突）；需要写操作时请对镜像文件使用。
- 授权横幅合并了所有被拒设备：一次授权即可全部可读。
- 如仍遇到系统级限制（如“完全磁盘访问权限”），可用 `sudo ./run.sh /dev/disk4s2` 直接以 root 运行。
- 建议先 `diskutil list` 确认分区对应的 `/dev/diskNsM`。

扫描会自动过滤系统合成卷（iSCPreboot/Recovery/系统卷等）与内置盘的 Apple 系统专用分区，
只展示真正有意义的设备；可移动设备排在最前面。

## 目录结构

```
e2fs/
├── src/bridge.c/.h     C 桥接层：open/list/stat/read/probe（JSON 输出，UTF-8 安全）
├── src/bridge_test.c   桥接层冒烟测试
├── backend/server.py   HTTP 后端：REST API、文件流（Range）、设备/镜像扫描
├── frontend/           文件管理器 UI（index.html / style.css / app.js，无框架）
├── webview/main.swift  原生 WKWebView 窗口
├── tests/make_images.sh  用 e2fsprogs 的 mke2fs/debugfs 造测试镜像
├── images/             测试镜像（ext2/ext3/ext4/notext）
├── run.sh              一键启动脚本
└── Makefile            E2FS=/path/to/e2fsprogs make all（可覆盖源码树位置）
```

## REST API 一览（127.0.0.1）

| 端点 | 说明 |
|---|---|
| `GET /api/info` | 当前文件系统信息（含 libext2fs 版本） |
| `POST /api/open` `{"path":...}` | 打开块设备/镜像（自动关闭上一个） |
| `POST /api/close` | 关闭当前文件系统 |
| `GET /api/list?path=/docs` | 列目录（含类型/大小/权限/mtime/符号链接目标） |
| `GET /api/stat?path=...` | 属性（resolved/raw 两份 inode 信息 + 链接目标） |
| `GET /api/file?path=...&max=N` | 文件内容（支持 `Range`，max 限制预览字节数） |
| `GET /api/download?path=...` | 以附件方式下载 |
| `GET /api/probe?path=...` | 探测是否 ext 及卷标/UUID |
| `GET /api/scan` | 扫描块设备（diskutil 拓扑：可移动/挂载状态/内容类型）与常见目录下的镜像 |
| `POST /api/elevate` `{"devices":[...]}` | 管理员授权读取块设备（osascript 弹系统密码框，chmod a+r） |
| `POST /api/mount` `{"device":...,"readOnly":bool}` | 挂载分区（diskutil mount） |
| `POST /api/unmount` `{"device":...}` | 卸载分区（若为当前打开设备，先写回缓冲并关闭会话） |
| `GET /api/mounts` | 轻量挂载状态（已挂载/可挂载分组 + 当前设备存在性，供轮询） |
| `POST /api/mkdir` `{"path":...}` | 新建目录 |
| `POST /api/rename` `{"path":...,"name":...}` | 同目录重命名（目标已存在报 409） |
| `POST /api/copy` `{"paths":[...],"target":dir}` | 复制（递归；冲突自动“副本”命名） |
| `POST /api/move` `{"paths":[...],"target":dir}` | 移动（冲突自动“2”命名；同目录跳过） |
| `GET /api/open-with?path=...` | 提取到临时目录并交给系统默认应用打开 |
| `POST /api/flush` | 把缓冲的改动按序写回磁盘 |
| `POST /api/close` | 卸载前自动写回全部缓冲改动（失败则保持打开） |
| `POST /api/check` `{"repair":bool}` | e2fsck 检查/修复（异步任务，repair 模式自动重开） |
| `POST /api/format` `{target,type,block_size,label,uuid,inode_size,reserved_pct,features,new_file,size_mb}` | mke2fs 格式化（异步任务；当前设备格式化后自动重开） |
| `POST /api/save` `{target,mode:"clone"\|"saveas",force}` | 保存到其他磁盘/文件（异步任务带进度） |
| `GET /api/job?id=...` | 轮询维护任务状态/输出/进度 |

## 已验证

- 桥接层只读：ext2/ext3/ext4 打开、中文文件名、符号链接（fast/slow）、硬链接、4MB 文件任意偏移读取、
  非 ext 文件与不存在路径的错误处理。
- 桥接层写入（`make test-write`）：建目录、同目录重命名、跨目录移动、目录递归复制（含子目录/图片/文本）、
  符号链接复制（目标保持）、冲突拒绝、移动目录后 `..` 指向新父目录、拒绝把目录移入自身子树、
  4MB 文件复制后逐字节比对——全部通过，且 `e2fsck -fn` 五个 Pass 全项干净。
- 端到端：REST API（curl）、文件下载逐字节一致、HTTP Range（206）、扫描结果（diskutil 拓扑融合、
  可移动设备/挂载点/内容类型）、冲突自动命名、只读打开后写端点返回 403、“用系统应用打开”的临时文件提取、
  挂载/卸载端点、`/dev/*` 打开被拒时返回 `need_permission`（前端据此发起授权流程）。
- **块设备访问实测**：将 ext4 镜像以 raw 磁盘设备挂接（`/dev/disk6`）后，
  浏览器以块设备方式成功打开并列出目录（强制只读）；FAT 镜像设备经 `/api/mount -readOnly`
  挂载到 `/Volumes/E2FSMNT` 后再卸载，全部通过。
- 浏览器 GUI 实测：F2 内联重命名（输入→Enter 生效）、`⌘C` 复制 + `⌘V` 粘贴、`⌘X` 剪切 + 粘贴移动、
  鼠标拖放把文件夹移入另一文件夹；对整个交互会话后的镜像跑 `e2fsck -fn` 依然全项干净。
- 原生窗口：进程启动、WKWebView 与后端建立连接正常（macOS 对 loopback 不启用 ATS 拦截）。
  如需对原生窗口截图观察，需在“系统设置 → 隐私与安全性 → 屏幕录制”中授权。
