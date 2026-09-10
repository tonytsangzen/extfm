/*
 * bridge.c — 基于 e2fsprogs libext2fs 的 ext2/3/4 文件系统只读浏览桥接层。
 *
 * 仅做只读访问，打开/读写全部经过全局互斥锁保护（HTTP 后端可能多线程调用）。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdarg.h>
#include <time.h>
#include <pthread.h>

#include "ext2fs/ext2fs.h"
#include "ext2fs/ext2_err.h"
#include "e2p/e2p.h"
#include "uuid/uuid.h"

#include "bridge.h"

/* 注册 libext2fs 错误表，让 error_message() 输出可读描述 */
__attribute__((constructor)) static void bridge_init(void)
{
    initialize_ext2_error_table();
}

/* ------------------------------------------------------------------ */
/* 动态字符串 / JSON 构建                                              */
/* ------------------------------------------------------------------ */

typedef struct {
    char  *buf;
    size_t len, cap;
    int    oom;                 /* 分配失败标记 */
} sb_t;

static void sb_grow(sb_t *s, size_t need)
{
    size_t want = s->len + need + 1;
    char *p;
    if (want <= s->cap)
        return;
    if (s->cap == 0)
        s->cap = 256;
    while (s->cap < want)
        s->cap *= 2;
    p = realloc(s->buf, s->cap);
    if (!p) {
        s->oom = 1;
        return;
    }
    s->buf = p;
}

static void sb_putn(sb_t *s, const char *src, size_t n)
{
    sb_grow(s, n);
    if (s->oom)
        return;
    memcpy(s->buf + s->len, src, n);
    s->len += n;
    s->buf[s->len] = 0;
}

static void sb_puts(sb_t *s, const char *str)
{
    if (str)
        sb_putn(s, str, strlen(str));
}

static void sb_putc(sb_t *s, char c)
{
    sb_putn(s, &c, 1);
}

static void sb_printf(sb_t *s, const char *fmt, ...)
{
    char tmp[512];
    va_list ap;
    int n;

    va_start(ap, fmt);
    n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    if (n < 0)
        return;
    if ((size_t)n < sizeof(tmp)) {
        sb_putn(s, tmp, (size_t)n);
        return;
    }
    {
        char *big = malloc((size_t)n + 1);
        if (!big) {
            s->oom = 1;
            return;
        }
        va_start(ap, fmt);
        vsnprintf(big, (size_t)n + 1, fmt, ap);
        va_end(ap);
        sb_putn(s, big, (size_t)n);
        free(big);
    }
}

/*
 * 追加一个 JSON 字符串字面量（含两侧引号）。
 * 输入按 UTF-8 校验，非法字节序列替换为 U+FFFD，保证 JSON 合法。
 */
static void sb_json_str(sb_t *s, const char *in, size_t len)
{
    const unsigned char *p = (const unsigned char *)in;
    size_t i = 0;

    sb_putc(s, '"');
    while (i < len) {
        unsigned char c = p[i];

        if (c == '"') {
            sb_puts(s, "\\\"");
            i++;
        } else if (c == '\\') {
            sb_puts(s, "\\\\");
            i++;
        } else if (c == '\n') {
            sb_puts(s, "\\n");
            i++;
        } else if (c == '\r') {
            sb_puts(s, "\\r");
            i++;
        } else if (c == '\t') {
            sb_puts(s, "\\t");
            i++;
        } else if (c < 0x20) {
            sb_printf(s, "\\u%04x", c);
            i++;
        } else if (c < 0x80) {
            sb_putc(s, (char)c);
            i++;
        } else {
            /* 多字节序列校验 */
            size_t seq = 0;
            unsigned cp = 0;
            if ((c & 0xE0) == 0xC0 && i + 1 < len &&
                (p[i + 1] & 0xC0) == 0x80) {
                seq = 2;
                cp = c & 0x1F;
            } else if ((c & 0xF0) == 0xE0 && i + 2 < len &&
                       (p[i + 1] & 0xC0) == 0x80 &&
                       (p[i + 2] & 0xC0) == 0x80) {
                seq = 3;
                cp = c & 0x0F;
            } else if ((c & 0xF8) == 0xF0 && i + 3 < len &&
                       (p[i + 1] & 0xC0) == 0x80 &&
                       (p[i + 2] & 0xC0) == 0x80 &&
                       (p[i + 3] & 0xC0) == 0x80) {
                seq = 4;
                cp = c & 0x07;
            }
            if (seq) {
                size_t k;
                for (k = 1; k < seq; k++)
                    cp = (cp << 6) | (p[i + k] & 0x3F);
                if (cp >= 0xD800 && cp <= 0xDFFF)
                    cp = 0xFFFD;
                {
                    int overlong = (seq == 2 && cp < 0x80) ||
                                   (seq == 3 && cp < 0x800) ||
                                   (seq == 4 && cp < 0x10000);
                    if (!overlong && cp <= 0x10FFFF) {
                        sb_putn(s, (const char *)p + i, seq);
                    } else {
                        sb_putn(s, "\xEF\xBF\xBD", 3);
                    }
                }
                i += seq;
            } else {
                sb_putn(s, "\xEF\xBF\xBD", 3);
                i++;
            }
        }
    }
    sb_putc(s, '"');
}

static char *sb_take(sb_t *s)
{
    char *out;
    if (s->oom || !s->buf) {
        free(s->buf);
        return NULL;
    }
    sb_putc(s, '\0');   /* 保底 NUL，buf 已含 */
    out = s->buf;
    memset(s, 0, sizeof(*s));
    return out;
}

/* ------------------------------------------------------------------ */
/* 工具                                                                */
/* ------------------------------------------------------------------ */

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static ext2_filsys g_fs = NULL;
static char *g_device = NULL;
static int g_writable = 0;      /* 以读写方式打开 */
static int g_clean = 0;         /* 超级块状态干净（允许写的前提） */

static void set_err(char **out_err, const char *fmt, ...)
{
    char tmp[1024];
    va_list ap;
    sb_t sb = {0};

    if (!out_err)
        return;
    va_start(ap, fmt);
    vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    sb_puts(&sb, tmp);
    *out_err = sb_take(&sb);
}

static const char *fs_strerror(errcode_t err)
{
    const char *m = error_message(err);
    return m ? m : "unknown error";
}

static const char *ftype_of(unsigned mode)
{
    switch (mode & LINUX_S_IFMT) {
    case LINUX_S_IFDIR:  return "dir";
    case LINUX_S_IFREG:  return "file";
    case LINUX_S_IFLNK:  return "symlink";
    case LINUX_S_IFCHR:  return "chardev";
    case LINUX_S_IFBLK:  return "blockdev";
    case LINUX_S_IFIFO:  return "fifo";
    case LINUX_S_IFSOCK: return "socket";
    default:            return "unknown";
    }
}

static void perms_str(unsigned mode, char out[11])
{
    static const char rwx[] = "rwx";
    int i;
    out[0] = '?';
    switch (mode & LINUX_S_IFMT) {
    case LINUX_S_IFDIR:  out[0] = 'd'; break;
    case LINUX_S_IFREG:  out[0] = '-'; break;
    case LINUX_S_IFLNK:  out[0] = 'l'; break;
    case LINUX_S_IFCHR:  out[0] = 'c'; break;
    case LINUX_S_IFBLK:  out[0] = 'b'; break;
    case LINUX_S_IFIFO:  out[0] = 'p'; break;
    case LINUX_S_IFSOCK: out[0] = 's'; break;
    }
    for (i = 0; i < 9; i++) {
        int bit = (mode >> (8 - i)) & 1;
        char base = rwx[i % 3];
        int special = 0;
        if (i == 2 && (mode & 04000))
            special = (base == 'x') ? 's' : 'S';
        else if (i == 5 && (mode & 02000))
            special = (base == 'x') ? 's' : 'S';
        else if (i == 8 && (mode & 01000))
            special = (base == 'x') ? 't' : 'T';
        out[1 + i] = bit ? (special ? special : base) : '-';
    }
    out[10] = 0;
}

static const char *fstype_of(ext2_filsys fs)
{
    struct ext2_super_block *sb = fs->super;

    if (!EXT2_HAS_COMPAT_FEATURE(sb, EXT3_FEATURE_COMPAT_HAS_JOURNAL))
        return "ext2";
    if (EXT2_HAS_INCOMPAT_FEATURE(sb, EXT3_FEATURE_INCOMPAT_EXTENTS |
                                       EXT4_FEATURE_INCOMPAT_64BIT |
                                       EXT4_FEATURE_INCOMPAT_FLEX_BG |
                                       EXT2_FEATURE_INCOMPAT_META_BG |
                                       EXT4_FEATURE_INCOMPAT_MMP |
                                       EXT4_FEATURE_INCOMPAT_ENCRYPT) ||
        EXT2_HAS_RO_COMPAT_FEATURE(sb, EXT4_FEATURE_RO_COMPAT_HUGE_FILE |
                                        EXT4_FEATURE_RO_COMPAT_DIR_NLINK |
                                        EXT4_FEATURE_RO_COMPAT_EXTRA_ISIZE |
                                        EXT4_FEATURE_RO_COMPAT_GDT_CSUM |
                                        EXT4_FEATURE_RO_COMPAT_METADATA_CSUM))
        return "ext4";
    return "ext3";
}

static const char *os_name(int os)
{
    switch (os) {
    case 0: return "Linux";
    case 1: return "Hurd";
    case 2: return "Masix";
    case 3: return "FreeBSD";
    case 4: return "Lites";
    default: return "Unknown";
    }
}

/* 追加 feature 名称列表（借助 libe2p） */
static void json_features(sb_t *s, struct ext2_super_block *sb)
{
    char fbuf[256];
    int grp, bit;

    sb_puts(s, "\"features\":{\"compat\":[");
    for (grp = 0; grp < 3; grp++) {
        unsigned mask;
        if (grp == 0)
            mask = sb->s_feature_compat;
        else if (grp == 1)
            mask = sb->s_feature_incompat;
        else
            mask = sb->s_feature_ro_compat;
        if (grp == 1)
            sb_puts(s, "],\"incompat\":[");
        if (grp == 2)
            sb_puts(s, "],\"ro_compat\":[");
        for (bit = 0; bit < 32; bit++) {
            if (mask & (1U << bit)) {
                memset(fbuf, 0, sizeof(fbuf));
                e2p_feature_to_string(grp, 1U << bit, fbuf, sizeof(fbuf));
                if (fbuf[0]) {
                    if (bit && (mask & ((1U << bit) - 1)))
                        sb_putc(s, ',');
                    sb_json_str(s, fbuf, strlen(fbuf));
                }
            }
        }
    }
    sb_puts(s, "]}");
}

/* 超级块公共字段 -> JSON（open/info/probe 共用） */
static void json_sb_common(sb_t *s, ext2_filsys fs, const char *device)
{
    struct ext2_super_block *sb = fs->super;
    unsigned long long blocksize = EXT2_BLOCK_SIZE(sb);
    unsigned long long blocks = ext2fs_blocks_count(sb);
    unsigned long long free_blocks = ext2fs_free_blocks_count(sb);
    unsigned long long inodes = sb->s_inodes_count;
    unsigned long long free_inodes = sb->s_free_inodes_count;
    char uuidbuf[40];
    char label[20];
    int n;

    uuid_unparse(sb->s_uuid, uuidbuf);
    for (n = 0; n < 16 && sb->s_volume_name[n]; n++)
        label[n] = sb->s_volume_name[n];
    label[n] = 0;

    sb_puts(s, "{\"device\":");
    sb_json_str(s, device, strlen(device));
    sb_printf(s, ",\"fstype\":\"%s\"", fstype_of(fs));
    sb_puts(s, ",\"label\":");
    sb_json_str(s, label, (size_t)n);
    sb_printf(s, ",\"uuid\":\"%s\"", uuidbuf);
    sb_printf(s, ",\"block_size\":%llu,\"blocks\":%llu,\"free_blocks\":%llu",
              blocksize, blocks, free_blocks);
    sb_printf(s, ",\"inodes\":%llu,\"free_inodes\":%llu", inodes, free_inodes);
    sb_printf(s, ",\"inode_size\":%u,\"capacity\":%llu,\"free_bytes\":%llu",
              sb->s_inode_size, blocks * blocksize, free_blocks * blocksize);
    sb_printf(s, ",\"state\":\"%s\"",
              (sb->s_state & EXT2_VALID_FS) ? "clean" : "unclean");
    sb_printf(s, ",\"creator_os\":\"%s\",\"revision\":%u,\"reserved_pct\":%u",
              os_name(sb->s_creator_os), sb->s_rev_level,
              (unsigned)(blocks ? sb->s_r_blocks_count * 100 / blocks : 0));
    sb_puts(s, ",\"last_mounted\":");
    sb_json_str(s, (const char *)sb->s_last_mounted, strnlen((const char *)sb->s_last_mounted, 64));
    sb_printf(s, ",\"mkfs_time\":%u,\"last_time\":%u,\"mounts\":%u,",
              sb->s_mkfs_time, sb->s_mtime, sb->s_mnt_count);
    sb_printf(s, "\"writable\":%s,\"clean\":%s,",
              (g_fs && g_writable && g_clean) ? "true" : "false",
              g_clean ? "true" : "false");
    json_features(s, sb);
    sb_putc(s, '}');
}

/* 单个 inode 的属性 -> JSON（不含 name/device 字段，由调用方补） */
static void json_inode(sb_t *s, ext2_filsys fs, ext2_ino_t ino,
                       struct ext2_inode *inode)
{
    unsigned long long size = EXT2_I_SIZE(inode);
    blk64_t blocks512 = ext2fs_inode_i_blocks(fs, inode);
    char perm[11];

    perms_str(inode->i_mode, perm);
    sb_printf(s, "\"ino\":%u,\"type\":\"%s\",\"size\":%llu",
              ino, ftype_of(inode->i_mode), size);
    sb_printf(s, ",\"mode\":%u,\"perms\":\"%s\"", inode->i_mode & 07777, perm);
    sb_printf(s, ",\"nlink\":%u,\"uid\":%u,\"gid\":%u",
              inode->i_links_count, inode->i_uid, inode->i_gid);
    sb_printf(s, ",\"atime\":%u,\"mtime\":%u,\"ctime\":%u",
              inode->i_atime, inode->i_mtime, inode->i_ctime);
    sb_printf(s, ",\"blocks_512\":%llu", (unsigned long long)blocks512);
}

/* 读取符号链接目标（fast/slow 两种） */
static int read_symlink_target(ext2_filsys fs, ext2_ino_t ino,
                               struct ext2_inode *inode, char **out,
                               char **out_err)
{
    unsigned long long len = EXT2_I_SIZE(inode);
    ext2_file_t file = NULL;
    unsigned int got = 0;
    char *buf;
    errcode_t err;

    *out = NULL;
    if (len == 0 || len > 4096) {
        set_err(out_err, "symlink target length out of range (%llu)", len);
        return -1;
    }
    buf = calloc(1, (size_t)len + 1);
    if (!buf) {
        set_err(out_err, "out of memory");
        return -1;
    }
    if (ext2fs_is_fast_symlink(inode)) {
        memcpy(buf, (char *)inode->i_block, (size_t)len);
    } else {
        err = ext2fs_file_open2(fs, ino, inode, 0, &file);
        if (err) {
            free(buf);
            set_err(out_err, "open symlink: %s", fs_strerror(err));
            return -1;
        }
        err = ext2fs_file_read(file, buf, (unsigned int)len, &got);
        ext2fs_file_close(file);
        if (err) {
            free(buf);
            set_err(out_err, "read symlink: %s", fs_strerror(err));
            return -1;
        }
    }
    *out = buf;
    return 0;
}

/* ------------------------------------------------------------------ */
/* 对外接口                                                            */
/* ------------------------------------------------------------------ */

static int open_impl(const char *device, int want_writable,
                     char **out_json, char **out_err)
{
    errcode_t err;
    ext2_filsys fs = NULL;
    sb_t sb = {0};
    int ret = -1;

    if (out_json)
        *out_json = NULL;
    if (out_err)
        *out_err = NULL;
    if (!device || !*device) {
        set_err(out_err, "device path is empty");
        return -1;
    }

    pthread_mutex_lock(&g_lock);
    /* 优先读写打开（支持写操作）；显式只读或 RW 失败时回退只读 */
    if (want_writable)
        err = ext2fs_open(device, EXT2_FLAG_RW | EXT2_FLAG_64BITS, 0, 0,
                          unix_io_manager, &fs);
    else
        err = EXT2_ET_FILE_NOT_FOUND;
    if (err)
        err = ext2fs_open(device, EXT2_FLAG_64BITS, 0, 0, unix_io_manager, &fs);
    if (err) {
        set_err(out_err, "无法打开 %s: %s (code %d)", device,
                fs_strerror(err), (int)err);
        goto out;
    }
    g_writable = (fs->flags & EXT2_FLAG_RW) ? 1 : 0;
    g_clean = (fs->super->s_state & EXT2_VALID_FS) ? 1 : 0;
    if (g_writable && g_clean) {
        /* 预加载位图，供后续分配 inode/块使用 */
        if (ext2fs_read_bitmaps(fs)) {
            g_writable = 0;
            g_clean = 0;
        }
    }

    free(g_device);
    g_device = strdup(device);
    if (g_fs)
        ext2fs_close(g_fs);
    g_fs = fs;

    json_sb_common(&sb, fs, device);
    if (!(*out_json = sb_take(&sb)))
        set_err(out_err, "out of memory");
    else
        ret = 0;
out:
    pthread_mutex_unlock(&g_lock);
    return ret;
}

int e2b_open(const char *device, char **out_json, char **out_err)
{
    return open_impl(device, 1, out_json, out_err);
}

int e2b_open_ro(const char *device, char **out_json, char **out_err)
{
    return open_impl(device, 0, out_json, out_err);
}

int e2b_close(void)
{
    pthread_mutex_lock(&g_lock);
    if (g_fs) {
        ext2fs_close(g_fs);
        g_fs = NULL;
    }
    g_writable = 0;
    g_clean = 0;
    free(g_device);
    g_device = NULL;
    pthread_mutex_unlock(&g_lock);
    return 0;
}

int e2b_writable(void)
{
    int v;
    pthread_mutex_lock(&g_lock);
    v = (g_fs && g_writable && g_clean);
    pthread_mutex_unlock(&g_lock);
    return v;
}

int e2b_is_open(void)
{
    int v;
    pthread_mutex_lock(&g_lock);
    v = g_fs != NULL;
    pthread_mutex_unlock(&g_lock);
    return v;
}

int e2b_info(char **out_json, char **out_err)
{
    sb_t sb = {0};
    int ret = -1;

    if (out_json)
        *out_json = NULL;
    if (out_err)
        *out_err = NULL;

    pthread_mutex_lock(&g_lock);
    if (!g_fs) {
        set_err(out_err, "no filesystem is open");
        goto out;
    }
    json_sb_common(&sb, g_fs, g_device);
    if (!(*out_json = sb_take(&sb)))
        set_err(out_err, "out of memory");
    else
        ret = 0;
out:
    pthread_mutex_unlock(&g_lock);
    return ret;
}

struct list_ctx {
    sb_t *sb;
    int count;
    int failed;
};

static int list_cb(ext2_ino_t dir EXT2FS_ATTR((unused)),
                   int entry EXT2FS_ATTR((unused)),
                   struct ext2_dir_entry *dirent,
                   int offset EXT2FS_ATTR((unused)),
                   int blocksize EXT2FS_ATTR((unused)),
                   char *buf EXT2FS_ATTR((unused)),
                   void *priv)
{
    struct list_ctx *ctx = priv;
    struct ext2_inode inode;
    int namelen;
    const char *name;

    if (!dirent || dirent->inode == 0)
        return 0;
    namelen = ext2fs_dirent_name_len(dirent);
    name = dirent->name;
    if (namelen <= 0 || (namelen == 1 && name[0] == '.') ||
        (namelen == 2 && name[0] == '.' && name[1] == '.'))
        return 0;

    if (ctx->count)
        sb_putc(ctx->sb, ',');
    sb_puts(ctx->sb, "{\"name\":");
    sb_json_str(ctx->sb, name, (size_t)namelen);
    sb_printf(ctx->sb, ",\"ino\":%u", dirent->inode);

    if (ext2fs_read_inode(g_fs, dirent->inode, &inode) == 0) {
        char perm[11];
        perms_str(inode.i_mode, perm);
        sb_printf(ctx->sb, ",\"type\":\"%s\",\"size\":%llu,\"mode\":%u",
                  ftype_of(inode.i_mode), EXT2_I_SIZE(&inode),
                  inode.i_mode & 07777);
        sb_printf(ctx->sb, ",\"perms\":\"%s\",\"mtime\":%u,\"nlink\":%u",
                  perm, inode.i_mtime, inode.i_links_count);
        if (LINUX_S_ISLNK(inode.i_mode)) {
            char *target = NULL;
            sb_puts(ctx->sb, ",\"target\":");
            if (read_symlink_target(g_fs, dirent->inode, &inode, &target,
                                    NULL) == 0 && target) {
                sb_json_str(ctx->sb, target, strlen(target));
                free(target);
            } else {
                sb_puts(ctx->sb, "null");
            }
        }
    } else {
        sb_puts(ctx->sb, ",\"type\":\"unknown\",\"size\":0,\"mode\":0,"
                         "\"perms\":\"\",\"mtime\":0,\"nlink\":0");
    }
    sb_putc(ctx->sb, '}');
    ctx->count++;
    return 0;
}

int e2b_list(const char *path, char **out_json, char **out_err)
{
    errcode_t err;
    ext2_ino_t ino;
    struct ext2_inode inode;
    char *blockbuf = NULL;
    struct list_ctx ctx;
    sb_t sb = {0};
    int ret = -1;

    if (out_json)
        *out_json = NULL;
    if (out_err)
        *out_err = NULL;

    pthread_mutex_lock(&g_lock);
    if (!g_fs) {
        set_err(out_err, "no filesystem is open");
        goto out;
    }
    err = ext2fs_namei_follow(g_fs, EXT2_ROOT_INO, EXT2_ROOT_INO, path, &ino);
    if (err) {
        set_err(out_err, "解析路径 %s 失败: %s", path, fs_strerror(err));
        goto out;
    }
    if (ext2fs_read_inode(g_fs, ino, &inode)) {
        set_err(out_err, "读取 inode 失败: %s", path);
        goto out;
    }
    if (!LINUX_S_ISDIR(inode.i_mode)) {
        set_err(out_err, "not a directory: %s", path);
        goto out;
    }

    blockbuf = malloc(g_fs->blocksize);
    if (!blockbuf) {
        set_err(out_err, "out of memory");
        goto out;
    }

    sb_printf(&sb, "{\"path\":");
    sb_json_str(&sb, path, strlen(path));
    sb_puts(&sb, ",\"count\":0,\"entries\":[");
    memset(&ctx, 0, sizeof(ctx));
    ctx.sb = &sb;

    err = ext2fs_dir_iterate2(g_fs, ino, 0, blockbuf, list_cb, &ctx);
    free(blockbuf);
    if (err) {
        set_err(out_err, "遍历目录失败: %s", fs_strerror(err));
        free(sb.buf);
        goto out;
    }
    sb_puts(&sb, "]}");
    {
        char *json = sb_take(&sb);
        if (!json) {
            set_err(out_err, "out of memory");
            goto out;
        }
        /* 回填 count：定位 '"count":0' 起始处替换 */
        {
            char *p = strstr(json, "\"count\":0");
            if (p) {
                char cnt[24];
                char *rest;
                size_t restlen, cntlen;
                snprintf(cnt, sizeof(cnt), "\"count\":%d", ctx.count);
                cntlen = strlen(cnt);
                rest = strdup(p + strlen("\"count\":0"));
                restlen = strlen(rest);
                memcpy(p, cnt, cntlen);
                memmove(p + cntlen, rest, restlen + 1);
                free(rest);
            }
        }
        *out_json = json;
        ret = 0;
    }
out:
    pthread_mutex_unlock(&g_lock);
    return ret;
}

int e2b_stat(const char *path, char **out_json, char **out_err)
{
    errcode_t err;
    ext2_ino_t raw_ino, res_ino;
    struct ext2_inode raw_inode, res_inode;
    char *target = NULL;
    int have_target = 0;
    sb_t sb = {0};
    int ret = -1;

    if (out_json)
        *out_json = NULL;
    if (out_err)
        *out_err = NULL;

    pthread_mutex_lock(&g_lock);
    if (!g_fs) {
        set_err(out_err, "no filesystem is open");
        goto out;
    }
    err = ext2fs_namei(g_fs, EXT2_ROOT_INO, EXT2_ROOT_INO, path, &raw_ino);
    if (err) {
        set_err(out_err, "解析路径 %s 失败: %s", path, fs_strerror(err));
        goto out;
    }
    if (ext2fs_read_inode(g_fs, raw_ino, &raw_inode)) {
        set_err(out_err, "读取 inode 失败: %s", path);
        goto out;
    }
    if (LINUX_S_ISLNK(raw_inode.i_mode)) {
        if (read_symlink_target(g_fs, raw_ino, &raw_inode, &target,
                                out_err) == 0)
            have_target = 1;
        else
            free(target), target = NULL;    /* 悬空/异常链接仍返回 stat */
    }

    err = ext2fs_namei_follow(g_fs, EXT2_ROOT_INO, EXT2_ROOT_INO, path,
                              &res_ino);
    sb_puts(&sb, "{\"path\":");
    sb_json_str(&sb, path, strlen(path));
    sb_puts(&sb, ",\"is_symlink\":");
    sb_puts(&sb, LINUX_S_ISLNK(raw_inode.i_mode) ? "true" : "false");
    sb_puts(&sb, ",\"symlink\":");
    if (have_target)
        sb_json_str(&sb, target, strlen(target));
    else
        sb_puts(&sb, "null");
    sb_puts(&sb, ",\"resolved\":");
    if (err == 0 && ext2fs_read_inode(g_fs, res_ino, &res_inode) == 0) {
        sb_putc(&sb, '{');
        json_inode(&sb, g_fs, res_ino, &res_inode);
        sb_putc(&sb, '}');
        ret = 0;
    } else if (LINUX_S_ISLNK(raw_inode.i_mode)) {
        /* 悬空符号链接：返回链接本身的信息 */
        sb_putc(&sb, '{');
        json_inode(&sb, g_fs, raw_ino, &raw_inode);
        sb_putc(&sb, '}');
        ret = 0;
    } else {
        set_err(out_err, "跟随路径失败: %s", fs_strerror(err));
        free(sb.buf);
        free(target);
        goto out;
    }
    sb_puts(&sb, ",\"raw\":{");
    json_inode(&sb, g_fs, raw_ino, &raw_inode);
    sb_puts(&sb, "}}");
    free(target);
    if (!(*out_json = sb_take(&sb))) {
        set_err(out_err, "out of memory");
        ret = -1;
    }
out:
    pthread_mutex_unlock(&g_lock);
    return ret;
}

int e2b_file_read(const char *path, long long off, long long len, void *buf,
                  long long *got, char **out_err)
{
    errcode_t err;
    ext2_ino_t ino;
    ext2_file_t file = NULL;
    unsigned long long size, pos, remaining;
    long long total = 0;
    int ret = -1;

    if (got)
        *got = 0;
    if (out_err)
        *out_err = NULL;
    if (!buf || len <= 0)
        return 0;

    pthread_mutex_lock(&g_lock);
    if (!g_fs) {
        set_err(out_err, "no filesystem is open");
        goto out;
    }
    err = ext2fs_namei_follow(g_fs, EXT2_ROOT_INO, EXT2_ROOT_INO, path, &ino);
    if (err) {
        set_err(out_err, "解析路径 %s 失败: %s", path, fs_strerror(err));
        goto out;
    }
    err = ext2fs_file_open2(g_fs, ino, NULL, 0, &file);
    if (err) {
        set_err(out_err, "打开文件失败: %s", fs_strerror(err));
        goto out;
    }
    {
        __u64 lsize = 0;
        err = ext2fs_file_get_lsize(file, &lsize);
        size = err ? 0 : lsize;
    }
    if (off < 0)
        off = 0;
    if ((unsigned long long)off >= size)
        goto done;
    remaining = size - (unsigned long long)off;
    if ((unsigned long long)len > remaining)
        len = (long long)remaining;

    pos = (unsigned long long)off;
    err = ext2fs_file_llseek(file, pos, EXT2_SEEK_SET, NULL);
    if (err) {
        set_err(out_err, "seek 失败: %s", fs_strerror(err));
        goto close_out;
    }
    while (total < len) {
        unsigned int chunk = (unsigned int)((len - total) > (1 << 20)
                                                ? (1 << 20)
                                                : (len - total));
        unsigned int gotn = 0;
        err = ext2fs_file_read(file, (char *)buf + total, chunk, &gotn);
        if (err) {
            set_err(out_err, "读取失败 @%llu: %s", pos + (unsigned)total,
                    fs_strerror(err));
            goto close_out;
        }
        if (gotn == 0)
            break;
        total += gotn;
    }
    ret = 0;
done:
    if (got)
        *got = total;
    ret = 0;
close_out:
    if (file)
        ext2fs_file_close(file);
out:
    pthread_mutex_unlock(&g_lock);
    return ret;
}

int e2b_probe(const char *device, char **out_json, char **out_err)
{
    errcode_t err;
    ext2_filsys fs = NULL;
    sb_t sb = {0};
    int ret = -1;

    if (out_json)
        *out_json = NULL;
    if (out_err)
        *out_err = NULL;

    /* probe 不动全局状态，但仍用锁保护 libext2fs 内部缓存 */
    pthread_mutex_lock(&g_lock);
    err = ext2fs_open(device, EXT2_FLAG_64BITS, 0, 0, unix_io_manager, &fs);
    if (err) {
        const char *m = fs_strerror(err);
        sb_puts(&sb, "{\"path\":");
        sb_json_str(&sb, device, strlen(device));
        sb_puts(&sb, ",\"ok\":false,\"error\":");
        sb_json_str(&sb, m, strlen(m));
        sb_printf(&sb, ",\"code\":%d}", (int)err);
        *out_json = sb_take(&sb);
        ret = 0;    /* 非 ext 或不可读：不算接口失败 */
        goto out;
    }
    {
        char uuidbuf[40], label[20];
        int n;

        for (n = 0; n < 16 && fs->super->s_volume_name[n]; n++)
            label[n] = fs->super->s_volume_name[n];
        label[n] = 0;
        uuid_unparse(fs->super->s_uuid, uuidbuf);

        sb_puts(&sb, "{\"path\":");
        sb_json_str(&sb, device, strlen(device));
        sb_puts(&sb, ",\"ok\":true");
        sb_printf(&sb, ",\"fstype\":\"%s\"", fstype_of(fs));
        sb_puts(&sb, ",\"label\":");
        sb_json_str(&sb, label, (size_t)n);
        sb_printf(&sb, ",\"uuid\":\"%s\"", uuidbuf);
        sb_printf(&sb, ",\"capacity\":%llu,\"block_size\":%u",
                  (unsigned long long)ext2fs_blocks_count(fs->super) *
                      EXT2_BLOCK_SIZE(fs->super),
                  EXT2_BLOCK_SIZE(fs->super));
        sb_putc(&sb, '}');
    }
    *out_json = sb_take(&sb);
    ret = 0;
    ext2fs_close(fs);
out:
    pthread_mutex_unlock(&g_lock);
    return ret;
}

void e2b_free(void *p)
{
    free(p);
}

/* ------------------------------------------------------------------ */
/* 写操作（重命名/移动、复制、建目录）                                  */
/* ------------------------------------------------------------------ */

static int ensure_writable(char **out_err)
{
    if (!g_fs) {
        set_err(out_err, "no filesystem is open");
        return -1;
    }
    if (!g_writable) {
        set_err(out_err, "文件系统以只读方式打开，无法执行写操作");
        return -1;
    }
    if (!g_clean) {
        set_err(out_err, "文件系统状态异常（未正常卸载），已拒绝写操作");
        return -1;
    }
    return 0;
}

/* 把路径拆成 父目录串 + 末段名字 */
static int split_path(const char *path, char **parent, const char **name)
{
    const char *slash = strrchr(path, '/');
    if (!slash || slash == path) {
        *parent = strdup("/");
        *name = path + (slash ? 1 : 0);
    } else {
        size_t plen = (size_t)(slash - path);
        char *p = malloc(plen + 1);
        if (!p)
            return -1;
        memcpy(p, path, plen);
        p[plen] = 0;
        *parent = p;
        *name = slash + 1;
    }
    if (!**name || !strcmp(*name, ".") || !strcmp(*name, "..")) {
        free(*parent);
        return -1;
    }
    return 0;
}

static int resolve_dir(const char *dirstr, ext2_ino_t *ino)
{
    return ext2fs_namei_follow(g_fs, EXT2_ROOT_INO, EXT2_ROOT_INO, dirstr,
                               ino);
}

/* 名字在目录中是否已存在。返回 1 存在 / 0 不存在 / -1 出错 */
static int name_exists(ext2_ino_t dir, const char *name)
{
    ext2_ino_t tmp;
    errcode_t err = ext2fs_lookup(g_fs, dir, name, strlen(name), NULL, &tmp);
    if (err == 0)
        return 1;
    if (err == EXT2_ET_FILE_NOT_FOUND)
        return 0;
    return -1;
}

static int ft_of_inode(struct ext2_inode *inode)
{
    switch (inode->i_mode & LINUX_S_IFMT) {
    case LINUX_S_IFDIR:  return EXT2_FT_DIR;
    case LINUX_S_IFREG:  return EXT2_FT_REG_FILE;
    case LINUX_S_IFLNK:  return EXT2_FT_SYMLINK;
    case LINUX_S_IFCHR:  return EXT2_FT_CHRDEV;
    case LINUX_S_IFBLK:  return EXT2_FT_BLKDEV;
    case LINUX_S_IFIFO:  return EXT2_FT_FIFO;
    case LINUX_S_IFSOCK: return EXT2_FT_SOCK;
    default:             return EXT2_FT_UNKNOWN;
    }
}

static errcode_t link_entry(ext2_ino_t dir, const char *name, ext2_ino_t ino,
                            int ft)
{
    errcode_t err = ext2fs_link(g_fs, dir, name, ino, ft);
    if (err == EXT2_ET_DIR_NO_SPACE) {
        err = ext2fs_expand_dir(g_fs, dir);
        if (err)
            return err;
        err = ext2fs_link(g_fs, dir, name, ino, ft);
    }
    return err;
}

static void bump_dir_mtime(ext2_ino_t dir)
{
    struct ext2_inode inode;
    if (ext2fs_read_inode(g_fs, dir, &inode) == 0) {
        inode.i_mtime = time(NULL);
        inode.i_ctime = inode.i_mtime;
        ext2fs_write_inode(g_fs, dir, &inode);
    }
}

/* 把 dir 的 ".." 指向 new_parent（直接改目录块；read/write_dir_block4 处理校验和） */
static errcode_t set_dotdot(ext2_ino_t dir, ext2_ino_t new_parent)
{
    struct ext2_inode inode;
    char *buf = NULL;
    blk64_t lblk, phys;
    unsigned long long size;
    errcode_t err;

    err = ext2fs_read_inode(g_fs, dir, &inode);
    if (err)
        return err;
    size = EXT2_I_SIZE(&inode);
    err = ext2fs_get_mem(g_fs->blocksize, &buf);
    if (err)
        return err;

    for (lblk = 0; (unsigned long long)lblk * g_fs->blocksize < size; lblk++) {
        unsigned int off = 0;
        int found = 0;

        err = ext2fs_bmap2(g_fs, dir, &inode, NULL, 0, lblk, NULL, &phys);
        if (err)
            goto out;
        if (!phys)
            continue;
        err = ext2fs_read_dir_block4(g_fs, phys, buf, 0, dir);
        if (err)
            goto out;
        while (off + 8 <= g_fs->blocksize) {
            struct ext2_dir_entry *de =
                (struct ext2_dir_entry *)(buf + off);
            unsigned int rec_len = de->rec_len;

            if (rec_len < 8 || off + rec_len > g_fs->blocksize)
                break;
            if (de->inode != 0) {
                int nl = ext2fs_dirent_name_len(de);
                if (nl == 2 && de->name[0] == '.' && de->name[1] == '.') {
                    de->inode = new_parent;
                    found = 1;
                }
            }
            off += rec_len;
        }
        if (found) {
            err = ext2fs_write_dir_block4(g_fs, phys, buf, 0, dir);
            goto out;   /* found: 0 或写失败 */
        }
    }
    err = EXT2_ET_FILE_NOT_FOUND;
out:
    ext2fs_free_mem(&buf);
    return err;
}

/* anc 是否为 dir 自身或其祖先（防止把目录移进自己的子树） */
static int is_self_or_ancestor(ext2_ino_t anc, ext2_ino_t dir)
{
    ext2_ino_t cur = dir;
    int depth = 0;

    while (depth++ < 1024) {
        if (cur == anc)
            return 1;
        if (cur == EXT2_ROOT_INO)
            return 0;
        if (ext2fs_lookup(g_fs, cur, "..", 2, NULL, &cur))
            return 0;
    }
    return 0;
}

/* 判断 new_dir 是否在待移动目录子树内（供跨目录移动用） */
static errcode_t flush_fs(char **out_err)
{
    errcode_t err = ext2fs_flush(g_fs);
    if (err)
        set_err(out_err, "写入超级块失败: %s", fs_strerror(err));
    return err;
}

int e2b_rename(const char *old_path, const char *new_path, char **out_err)
{
    char *old_parent = NULL, *new_parent = NULL;
    const char *old_name, *new_name;
    ext2_ino_t od, nd, ino;
    struct ext2_inode inode, pinode;
    int ret = -1;

    pthread_mutex_lock(&g_lock);
    if (ensure_writable(out_err))
        goto out;
    if (split_path(old_path, &old_parent, &old_name) ||
        split_path(new_path, &new_parent, &new_name)) {
        set_err(out_err, "路径不合法: %s / %s", old_path, new_path);
        goto out;
    }
    if (resolve_dir(old_parent, &od) || resolve_dir(new_parent, &nd)) {
        set_err(out_err, "父目录解析失败: %s -> %s", old_path, new_path);
        goto out;
    }
    if (ext2fs_lookup(g_fs, od, old_name, strlen(old_name), NULL, &ino)) {
        set_err(out_err, "源不存在: %s", old_path);
        goto out;
    }
    if (ext2fs_read_inode(g_fs, ino, &inode)) {
        set_err(out_err, "读取 inode 失败");
        goto out;
    }
    if (od == nd && !strcmp(old_name, new_name)) {
        ret = 0;    /* 同目录同名：无操作 */
        goto out;
    }
    {
        int ex = name_exists(nd, new_name);
        if (ex < 0) {
            set_err(out_err, "检查目标名称失败");
            goto out;
        }
        if (ex) {
            set_err(out_err, "目标已存在: %s", new_path);
            goto out;
        }
    }
    if (LINUX_S_ISDIR(inode.i_mode)) {
        if (is_self_or_ancestor(ino, nd)) {
            set_err(out_err, "不能把目录移动到它自己或其子目录内");
            goto out;
        }
    }
    if (ext2fs_unlink(g_fs, od, old_name, 0, 0)) {
        set_err(out_err, "移除原目录项失败: %s", old_path);
        goto out;
    }
    if (link_entry(nd, new_name, ino, ft_of_inode(&inode))) {
        /* 尽力恢复原目录项 */
        ext2fs_link(g_fs, od, old_name, ino, ft_of_inode(&inode));
        set_err(out_err, "写入新目录项失败: %s", new_path);
        goto out;
    }
    if (LINUX_S_ISDIR(inode.i_mode) && nd != od) {
        if (set_dotdot(ino, nd)) {
            set_err(out_err, "修复 \"..\" 失败");
            goto out;
        }
        if (ext2fs_read_inode(g_fs, od, &pinode) == 0) {
            pinode.i_links_count--;
            ext2fs_write_inode(g_fs, od, &pinode);
        }
        if (ext2fs_read_inode(g_fs, nd, &pinode) == 0) {
            pinode.i_links_count++;
            ext2fs_write_inode(g_fs, nd, &pinode);
        }
        {
            struct ext2_inode si;
            if (ext2fs_read_inode(g_fs, ino, &si) == 0) {
                si.i_ctime = time(NULL);
                ext2fs_write_inode(g_fs, ino, &si);
            }
        }
    }
    bump_dir_mtime(od);
    if (nd != od)
        bump_dir_mtime(nd);
    if (flush_fs(out_err) == 0)
        ret = 0;
out:
    free(old_parent);
    free(new_parent);
    pthread_mutex_unlock(&g_lock);
    return ret;
}

/* ---- 复制 ---- */

static errcode_t write_span(ext2_file_t f, const char *buf, unsigned int len)
{
    errcode_t err;
    while (len > 0) {
        unsigned int written = 0;
        err = ext2fs_file_write(f, buf, len, &written);
        if (err)
            return err;
        if (written == 0)
            return EXT2_ET_SHORT_WRITE;
        buf += written;
        len -= written;
    }
    return 0;
}

/* 按块写入，全零块跳过（保留稀疏性） */
static errcode_t copy_file_data(ext2_ino_t src_ino,
                                struct ext2_inode *src_inode, ext2_file_t dst)
{
    unsigned long long size = EXT2_I_SIZE(src_inode);
    char *buf = NULL, *zero = NULL;
    ext2_file_t src = NULL;
    unsigned long long off;
    errcode_t err;

    err = ext2fs_file_open2(g_fs, src_ino, NULL, 0, &src);
    if (err)
        return err;
    err = ext2fs_get_mem(1 << 20, &buf);
    if (err)
        goto out;
    err = ext2fs_get_memzero(g_fs->blocksize, &zero);
    if (err)
        goto out;

    for (off = 0; off < size; off += (1 << 20)) {
        unsigned int want = (size - off) > (1 << 20) ? (1 << 20)
                                                     : (unsigned int)(size - off);
        unsigned int got = 0;
        unsigned int bpos;
        err = ext2fs_file_llseek(src, off, EXT2_SEEK_SET, NULL);
        if (err)
            goto out;
        err = ext2fs_file_read(src, buf, want, &got);
        if (err)
            goto out;
        for (bpos = 0; bpos < got; bpos += g_fs->blocksize) {
            unsigned int blen = g_fs->blocksize;
            if (blen > got - bpos)
                blen = got - bpos;
            if (memcmp(buf + bpos, zero, blen) == 0)
                continue;
            err = write_span(dst, buf + bpos, blen);
            if (err)
                goto out;
        }
    }
    err = ext2fs_file_set_size2(dst, size);
out:
    if (src)
        ext2fs_file_close(src);
    ext2fs_free_mem(&buf);
    ext2fs_free_mem(&zero);
    return err;
}

static errcode_t make_new_file_inode(ext2_ino_t parent, unsigned mode,
                                     unsigned long long size, ext2_ino_t *ino,
                                     struct ext2_inode *inode)
{
    errcode_t err;
    time_t now = time(NULL);

    err = ext2fs_new_inode(g_fs, parent, 010644, 0, ino);
    if (err)
        return err;
    memset(inode, 0, sizeof(*inode));
    inode->i_mode = mode;
    inode->i_links_count = 1;
    ext2fs_inode_xtime_set(inode, i_atime, now);
    ext2fs_inode_xtime_set(inode, i_ctime, now);
    ext2fs_inode_xtime_set(inode, i_mtime, now);
    err = ext2fs_inode_size_set(g_fs, inode, size);
    if (err)
        return err;
    if (ext2fs_has_feature_extents(g_fs->super)) {
        ext2_extent_handle_t handle;
        inode->i_flags &= ~EXT4_EXTENTS_FL;
        err = ext2fs_extent_open2(g_fs, *ino, inode, &handle);
        if (err)
            return err;
        ext2fs_extent_free(handle);
    }
    err = ext2fs_write_new_inode(g_fs, *ino, inode);
    if (err)
        return err;
    ext2fs_inode_alloc_stats2(g_fs, *ino, +1, 0);
    return 0;
}

static errcode_t copy_regular(ext2_ino_t src_ino, struct ext2_inode *src_inode,
                              ext2_ino_t dst_dir, const char *new_name)
{
    struct ext2_inode newi;
    ext2_ino_t new_ino;
    ext2_file_t dst = NULL;
    errcode_t err;

    err = make_new_file_inode(dst_dir,
                              (src_inode->i_mode & 07777) | LINUX_S_IFREG,
                              EXT2_I_SIZE(src_inode), &new_ino, &newi);
    if (err)
        return err;
    err = link_entry(dst_dir, new_name, new_ino, EXT2_FT_REG_FILE);
    if (err)
        return err;
    err = ext2fs_file_open2(g_fs, new_ino, NULL,
                            EXT2_FILE_WRITE | EXT2_FILE_CREATE, &dst);
    if (err)
        return err;
    err = copy_file_data(src_ino, src_inode, dst);
    {
        errcode_t cerr = ext2fs_file_close(dst);
        if (!err)
            err = cerr;
    }
    return err;
}

static errcode_t copy_special(ext2_ino_t dst_dir, struct ext2_inode *src_inode,
                              const char *new_name)
{
    struct ext2_inode newi;
    ext2_ino_t new_ino;
    errcode_t err;

    err = ext2fs_new_inode(g_fs, dst_dir, 010644, 0, &new_ino);
    if (err)
        return err;
    memset(&newi, 0, sizeof(newi));
    newi.i_mode = src_inode->i_mode;    /* 含类型位 */
    newi.i_links_count = 1;
    newi.i_atime = newi.i_ctime = newi.i_mtime = time(NULL);
    if ((src_inode->i_mode & LINUX_S_IFMT) == LINUX_S_IFCHR ||
        (src_inode->i_mode & LINUX_S_IFMT) == LINUX_S_IFBLK)
        newi.i_block[0] = src_inode->i_block[0];   /* 设备号 */
    err = ext2fs_write_new_inode(g_fs, new_ino, &newi);
    if (err)
        return err;
    ext2fs_inode_alloc_stats2(g_fs, new_ino, +1, 0);
    return link_entry(dst_dir, new_name, new_ino, ft_of_inode(src_inode));
}

static errcode_t copy_recursive(ext2_ino_t src_ino, struct ext2_inode *src_inode,
                                ext2_ino_t dst_dir, const char *new_name,
                                int depth);

/* ---- 目录项收集（先收集后处理，避免迭代中修改目录的风险） ---- */
struct name_ent {
    char *name;
    ext2_ino_t ino;
};
struct name_list {
    struct name_ent *v;
    int n, cap;
    errcode_t err;
};

static int collect_cb(ext2_ino_t dir EXT2FS_ATTR((unused)),
                      int entry EXT2FS_ATTR((unused)),
                      struct ext2_dir_entry *dirent,
                      int offset EXT2FS_ATTR((unused)),
                      int blocksize EXT2FS_ATTR((unused)),
                      char *buf EXT2FS_ATTR((unused)), void *priv)
{
    struct name_list *l = priv;
    int nl;

    if (!dirent || dirent->inode == 0)
        return 0;
    nl = ext2fs_dirent_name_len(dirent);
    if (nl <= 0)
        return 0;
    if ((nl == 1 && dirent->name[0] == '.') ||
        (nl == 2 && dirent->name[0] == '.' && dirent->name[1] == '.'))
        return 0;
    if (l->n == l->cap) {
        int ncap = l->cap ? l->cap * 2 : 32;
        struct name_ent *nv = realloc(l->v, ncap * sizeof(*nv));
        if (!nv) {
            l->err = EXT2_ET_NO_MEMORY;
            return DIRENT_ABORT;
        }
        l->v = nv;
        l->cap = ncap;
    }
    l->v[l->n].name = malloc((size_t)nl + 1);
    if (!l->v[l->n].name) {
        l->err = EXT2_ET_NO_MEMORY;
        return DIRENT_ABORT;
    }
    memcpy(l->v[l->n].name, dirent->name, (size_t)nl);
    l->v[l->n].name[nl] = 0;
    l->v[l->n].ino = dirent->inode;
    l->n++;
    return 0;
}

static errcode_t collect_dir(ext2_ino_t dir, struct name_list *l)
{
    char *blockbuf;
    errcode_t err;

    memset(l, 0, sizeof(*l));
    blockbuf = malloc(g_fs->blocksize);
    if (!blockbuf)
        return EXT2_ET_NO_MEMORY;
    err = ext2fs_dir_iterate2(g_fs, dir, 0, blockbuf, collect_cb, l);
    free(blockbuf);
    if (err)
        err = l->err ? l->err : err;
    return err;
}

static void free_name_list(struct name_list *l)
{
    int i;
    for (i = 0; i < l->n; i++)
        free(l->v[i].name);
    free(l->v);
    l->v = NULL;
    l->n = l->cap = 0;
}

static errcode_t copy_recursive(ext2_ino_t src_ino, struct ext2_inode *src_inode,
                                ext2_ino_t dst_dir, const char *new_name,
                                int depth)
{
    struct name_list list;
    ext2_ino_t new_dir_ino = 0;
    errcode_t err;
    int i;

    if (depth > 64)
        return EXT2_ET_INVALID_ARGUMENT;
    err = ext2fs_mkdir(g_fs, dst_dir, 0, new_name);
    if (err)
        return err;
    if (ext2fs_lookup(g_fs, dst_dir, new_name, strlen(new_name),
                      NULL, &new_dir_ino))
        return EXT2_ET_FILE_NOT_FOUND;
    /* 对齐源目录权限 */
    {
        struct ext2_inode ni;
        if (ext2fs_read_inode(g_fs, new_dir_ino, &ni) == 0) {
            ni.i_mode = (ni.i_mode & LINUX_S_IFMT) |
                        (src_inode->i_mode & 07777);
            ni.i_atime = ni.i_ctime = ni.i_mtime = time(NULL);
            ext2fs_write_inode(g_fs, new_dir_ino, &ni);
        }
    }

    err = collect_dir(src_ino, &list);
    if (err)
        return err;

    for (i = 0; i < list.n; i++) {
        struct ext2_inode ci;
        const char *nm = list.v[i].name;

        if (ext2fs_read_inode(g_fs, list.v[i].ino, &ci)) {
            err = EXT2_ET_FILE_NOT_FOUND;
            break;
        }
        switch (ci.i_mode & LINUX_S_IFMT) {
        case LINUX_S_IFREG:
            err = copy_regular(list.v[i].ino, &ci, new_dir_ino, nm);
            break;
        case LINUX_S_IFLNK: {
            char *target = NULL;
            if (read_symlink_target(g_fs, list.v[i].ino, &ci, &target,
                                    NULL) == 0 && target) {
                err = ext2fs_symlink(g_fs, new_dir_ino, 0, nm, target);
                free(target);
            } else {
                err = EXT2_ET_INVALID_ARGUMENT;
            }
            break;
        }
        case LINUX_S_IFDIR:
            err = copy_recursive(list.v[i].ino, &ci, new_dir_ino, nm,
                                 depth + 1);
            break;
        case LINUX_S_IFCHR:
        case LINUX_S_IFBLK:
        case LINUX_S_IFIFO:
            err = copy_special(new_dir_ino, &ci, nm);
            break;
        default:
            err = 0;    /* socket 等跳过 */
            break;
        }
        if (err)
            break;
    }
    free_name_list(&list);
    if (!err)
        bump_dir_mtime(new_dir_ino);
    return err;
}

int e2b_copy(const char *old_path, const char *new_path, char **out_err)
{
    char *old_parent = NULL, *new_parent = NULL;
    const char *old_name, *new_name;
    ext2_ino_t od, nd, ino;
    struct ext2_inode inode;
    errcode_t err = 0;
    int ret = -1;

    pthread_mutex_lock(&g_lock);
    if (ensure_writable(out_err))
        goto out;
    if (split_path(old_path, &old_parent, &old_name) ||
        split_path(new_path, &new_parent, &new_name)) {
        set_err(out_err, "路径不合法: %s / %s", old_path, new_path);
        goto out;
    }
    if (resolve_dir(old_parent, &od) || resolve_dir(new_parent, &nd)) {
        set_err(out_err, "父目录解析失败: %s -> %s", old_path, new_path);
        goto out;
    }
    if (ext2fs_lookup(g_fs, od, old_name, strlen(old_name), NULL, &ino)) {
        set_err(out_err, "源不存在: %s", old_path);
        goto out;
    }
    if (ext2fs_read_inode(g_fs, ino, &inode)) {
        set_err(out_err, "读取 inode 失败");
        goto out;
    }
    {
        int ex = name_exists(nd, new_name);
        if (ex > 0) {
            set_err(out_err, "目标已存在: %s", new_path);
            goto out;
        }
        if (ex < 0) {
            set_err(out_err, "检查目标名称失败");
            goto out;
        }
    }
    switch (inode.i_mode & LINUX_S_IFMT) {
    case LINUX_S_IFREG:
        err = copy_regular(ino, &inode, nd, new_name);
        break;
    case LINUX_S_IFLNK: {
        char *target = NULL;
        if (read_symlink_target(g_fs, ino, &inode, &target, out_err) == 0 &&
            target) {
            err = ext2fs_symlink(g_fs, nd, 0, new_name, target);
            free(target);
        } else {
            err = EXT2_ET_INVALID_ARGUMENT;
        }
        break;
    }
    case LINUX_S_IFDIR:
        if (is_self_or_ancestor(ino, nd)) {
            set_err(out_err, "不能把目录复制到它自己或其子目录内");
            goto out;
        }
        err = copy_recursive(ino, &inode, nd, new_name, 0);
        break;
    case LINUX_S_IFCHR:
    case LINUX_S_IFBLK:
    case LINUX_S_IFIFO:
        err = copy_special(nd, &inode, new_name);
        break;
    default:
        set_err(out_err, "不支持复制该类型");
        goto out;
    }
    if (err) {
        set_err(out_err, "复制失败: %s", fs_strerror(err));
        goto out;
    }
    bump_dir_mtime(nd);
    if (flush_fs(out_err) == 0)
        ret = 0;
out:
    free(old_parent);
    free(new_parent);
    pthread_mutex_unlock(&g_lock);
    return ret;
}

/* ---- 删除 ---- */

static int release_blocks_proc(ext2_filsys fs EXT2FS_ATTR((unused)),
                               blk64_t *blocknr,
                               e2_blkcnt_t blockcnt EXT2FS_ATTR((unused)),
                               blk64_t ref_block EXT2FS_ATTR((unused)),
                               int ref_offset EXT2FS_ATTR((unused)),
                               void *priv EXT2FS_ATTR((unused)))
{
    ext2fs_block_alloc_stats(fs, *blocknr, -1);
    return 0;
}

/* 释放 inode 的数据块并注销统计（debugfs kill_file_by_inode 模式） */
static errcode_t kill_inode_blocks(ext2_ino_t ino, struct ext2_inode *inode)
{
    errcode_t err;

    inode->i_links_count = 0;   /* 彻底删除：链接数归零，避免 dtime 与链接并存 */
    ext2fs_set_dtime(g_fs, inode);
    err = ext2fs_write_inode(g_fs, ino, inode);
    if (err)
        return err;
    if (ext2fs_inode_has_valid_blocks2(g_fs, inode))
        err = ext2fs_block_iterate3(g_fs, ino, BLOCK_FLAG_READ_ONLY, NULL,
                                    release_blocks_proc, NULL);
    ext2fs_inode_alloc_stats2(g_fs, ino, -1, LINUX_S_ISDIR(inode->i_mode));
    return err;
}

static errcode_t delete_dir_children(ext2_ino_t dir, int depth);

static errcode_t delete_child(ext2_ino_t parent, const char *name,
                              ext2_ino_t ino, int depth)
{
    struct ext2_inode ci;
    errcode_t err;

    if (ext2fs_read_inode(g_fs, ino, &ci))
        return EXT2_ET_FILE_NOT_FOUND;
    if (LINUX_S_ISDIR(ci.i_mode)) {
        if (depth > 64)
            return EXT2_ET_INVALID_ARGUMENT;
        err = delete_dir_children(ino, depth + 1);
        if (err)
            return err;
    }
    err = kill_inode_blocks(ino, &ci);
    if (err)
        return err;
    return ext2fs_unlink(g_fs, parent, name, 0, 0);
}

static errcode_t delete_dir_children(ext2_ino_t dir, int depth)
{
    struct name_list list;
    errcode_t err;
    int i;

    if (depth > 64)
        return EXT2_ET_INVALID_ARGUMENT;
    err = collect_dir(dir, &list);
    if (err)
        return err;
    for (i = 0; i < list.n; i++) {
        err = delete_child(dir, list.v[i].name, list.v[i].ino, depth);
        if (err)
            break;
    }
    free_name_list(&list);
    return err;
}

int e2b_delete(const char *path, int recursive, char **out_err)
{
    char *parent = NULL;
    const char *name;
    ext2_ino_t pd, ino;
    struct ext2_inode inode, pinode;
    errcode_t err;
    int ret = -1;

    pthread_mutex_lock(&g_lock);
    if (ensure_writable(out_err))
        goto out;
    if (!path || !strcmp(path, "/")) {
        set_err(out_err, "不能删除根目录");
        goto out;
    }
    if (split_path(path, &parent, &name)) {
        set_err(out_err, "路径不合法: %s", path);
        goto out;
    }
    if (resolve_dir(parent, &pd)) {
        set_err(out_err, "父目录不存在: %s", parent);
        goto out;
    }
    if (ext2fs_lookup(g_fs, pd, name, strlen(name), NULL, &ino)) {
        set_err(out_err, "源不存在: %s", path);
        goto out;
    }
    if (ext2fs_read_inode(g_fs, ino, &inode)) {
        set_err(out_err, "读取 inode 失败");
        goto out;
    }
    if (LINUX_S_ISDIR(inode.i_mode)) {
        /* 目录：非空且未指定递归时拒绝 */
        if (!recursive) {
            struct name_list l;
            errcode_t lerr = collect_dir(ino, &l);
            int n = lerr ? -1 : l.n;
            free_name_list(&l);
            if (n < 0) {
                set_err(out_err, "检查目录内容失败");
                goto out;
            }
            if (n > 0) {
                set_err(out_err, "目录非空（需递归删除）");
                goto out;
            }
        }
        err = delete_dir_children(ino, 0);
        if (err) {
            set_err(out_err, "删除目录内容失败: %s", fs_strerror(err));
            goto out;
        }
    }
    err = ext2fs_unlink(g_fs, pd, name, 0, 0);
    if (err) {
        set_err(out_err, "移除目录项失败: %s", fs_strerror(err));
        goto out;
    }
    err = kill_inode_blocks(ino, &inode);
    if (err) {
        set_err(out_err, "释放 inode 失败: %s", fs_strerror(err));
        goto out;
    }
    if (LINUX_S_ISDIR(inode.i_mode) && pd != ino) {
        if (ext2fs_read_inode(g_fs, pd, &pinode) == 0 &&
            pinode.i_links_count > 1) {
            pinode.i_links_count--;
            ext2fs_write_inode(g_fs, pd, &pinode);
        }
    }
    bump_dir_mtime(pd);
    if (flush_fs(out_err) == 0)
        ret = 0;
out:
    free(parent);
    pthread_mutex_unlock(&g_lock);
    return ret;
}


int e2b_mkdir(const char *path, char **out_err)
{
    char *parent = NULL;
    const char *name;
    ext2_ino_t pd;
    int ret = -1;

    pthread_mutex_lock(&g_lock);
    if (ensure_writable(out_err))
        goto out;
    if (split_path(path, &parent, &name)) {
        set_err(out_err, "路径不合法: %s", path);
        goto out;
    }
    if (resolve_dir(parent, &pd)) {
        set_err(out_err, "父目录不存在: %s", parent);
        goto out;
    }
    {
        int ex = name_exists(pd, name);
        if (ex > 0) {
            set_err(out_err, "目标已存在: %s", path);
            goto out;
        }
        if (ex < 0) {
            set_err(out_err, "检查目标名称失败");
            goto out;
        }
    }
    if (ext2fs_mkdir(g_fs, pd, 0, name)) {
        set_err(out_err, "创建目录失败: %s", path);
        goto out;
    }
    bump_dir_mtime(pd);
    if (flush_fs(out_err) == 0)
        ret = 0;
out:
    free(parent);
    pthread_mutex_unlock(&g_lock);
    return ret;
}

const char *e2b_version(void)
{
    const char *ver = NULL;
    ext2fs_get_library_version(&ver, NULL);
    return ver ? ver : "unknown";
}
