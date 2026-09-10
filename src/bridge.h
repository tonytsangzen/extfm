/*
 * bridge.h — 简化的 libext2fs 封装接口（供 Python/ctypes 等外部调用方使用）
 *
 * 约定：
 *  - 同一时刻至多打开一个文件系统（open 会替换旧的）
 *  - 所有 out_json / out_err 为 malloc 缓冲区，调用方用 e2b_free() 释放
 *  - 路径为文件系统内绝对路径（UTF-8）；返回 JSON 均为合法 UTF-8
 *  - 返回值 0 = 成功；非 0 = 失败（out_err 给出错误描述）
 */
#ifndef E2FS_BRIDGE_H
#define E2FS_BRIDGE_H

#ifdef __cplusplus
extern "C" {
#endif

/* 打开块设备或 raw 镜像文件（优先读写；成功时 out_json 返回文件系统信息）。
   e2b_open_ro 强制只读。失败时 out_err 给出错误描述 */
int e2b_open(const char *device, char **out_json, char **out_err);
int e2b_open_ro(const char *device, char **out_json, char **out_err);

/* 关闭当前文件系统 */
int e2b_close(void);

/* 是否已打开 */
int e2b_is_open(void);

/* 当前文件系统信息（未打开返回错误） */
int e2b_info(char **out_json, char **out_err);

/* 列出目录内容 */
int e2b_list(const char *path, char **out_json, char **out_err);

/* 查询文件/目录属性（符号链接给出目标但 stat 跟随后的信息） */
int e2b_stat(const char *path, char **out_json, char **out_err);

/* 读取文件内容片段。返回实际读取字节数（-1 出错），got 返回成功读取数 */
int e2b_file_read(const char *path, long long off, long long len,
                  void *buf, long long *got, char **out_err);

/* 探测设备/镜像是否为 ext2/3/4（只读试开，随即关闭，不影响当前会话） */
int e2b_probe(const char *device, char **out_json, char **out_err);

/* ---- 写操作（打开时具备写权限且文件系统状态干净才可用） ---- */

/* 当前文件系统是否可写 */
int e2b_writable(void);

/* 创建目录 */
int e2b_mkdir(const char *path, char **out_err);

/* 重命名/移动（支持跨目录；new 已存在则报错） */
int e2b_rename(const char *old_path, const char *new_path, char **out_err);

/* 复制（文件/符号链接/目录递归；new 已存在则报错） */
int e2b_copy(const char *old_path, const char *new_path, char **out_err);

/* 删除（文件/符号链接/目录递归）。recursive=0 时非空目录报错 */
int e2b_delete(const char *path, int recursive, char **out_err);

/* 释放桥接层返回的缓冲区 */
void e2b_free(void *p);

/* libext2fs 版本字符串 */
const char *e2b_version(void);

#ifdef __cplusplus
}
#endif
#endif /* E2FS_BRIDGE_H */
