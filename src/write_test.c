/* write_test.c — 桥接层写操作测试：建目录/复制/重命名/移动，配合 e2fsck 校验 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "bridge.h"

static int failures = 0;

static void expect(const char *what, int cond)
{
    printf("[%s] %s\n", cond ? "PASS" : "FAIL", what);
    if (!cond)
        failures++;
}

static char *call(const char *what, int rc, char *json, char *err)
{
    if (rc != 0) {
        printf("FAIL %s: %s\n", what, err ? err : "?");
        failures++;
        e2b_free(err);
        return NULL;
    }
    printf("ok   %s\n", what);
    return json;    /* 调用方负责 e2b_free */
}

static void expect_read(const char *path, const char *needle)
{
    char buf[4096] = {0}, *err = NULL;
    long long got = 0;
    int rc = e2b_file_read(path, 0, sizeof(buf) - 1, buf, &got, &err);
    if (rc != 0) {
        printf("FAIL read %s: %s\n", path, err ? err : "?");
        e2b_free(err);
        failures++;
        return;
    }
    expect("read-back contains marker", strstr(buf, needle) != NULL);
}

int main(int argc, char **argv)
{
    const char *img = argc > 1 ? argv[1] : "build/wtest.img";
    char *json, *err;

    expect("open writable", e2b_open(img, &json, &err) == 0);
    if (json) {
        expect("writable flag reported", strstr(json, "\"writable\":true") != NULL);
        e2b_free(json);
    }

    expect("mkdir /copydst", e2b_mkdir("/copydst", &err) == 0);
    expect("mkdir exists check", e2b_mkdir("/copydst", &err) != 0);

    /* 递归目录复制（含子目录、图片、文本） */
    expect("copy dir /docs -> /copydst/docs", e2b_copy("/docs", "/copydst/docs", &err) == 0);
    expect("copy file -> /copydst/说明副本.txt", e2b_copy("/docs/说明.txt", "/copydst/说明副本.txt", &err) == 0);
    expect("copy conflict rejected", e2b_copy("/docs/说明.txt", "/copydst/说明副本.txt", &err) != 0);
    expect("copy big.bin", e2b_copy("/big.bin", "/copydst/big2.bin", &err) == 0);
    expect("copy symlink", e2b_copy("/软链接.txt", "/copydst/link-copy.txt", &err) == 0);

    /* 内容与属性核对 */
    {
        char *stat_json = NULL;
        int rc = e2b_stat("/copydst/link-copy.txt", &stat_json, &err);
        json = call("stat copied symlink", rc, stat_json, err);
        if (json) {
            expect("copied symlink keeps target",
                   strstr(json, "/docs/说明.txt") != NULL);
            e2b_free(json);
        }
    }
    expect_read("/copydst/docs/说明.txt", "quick brown fox");
    expect_read("/copydst/docs/子目录/../说明.txt", "quick brown fox");
    {
        char *st = NULL;
        int rc = e2b_stat("/copydst/big2.bin", &st, &err);
        json = call("stat copied big file", rc, st, err);
        if (json) {
            expect("big copy size matches", strstr(json, "\"size\":4194304") != NULL);
            e2b_free(json);
        }
    }
    {
        char a[64] = {0}, b[64] = {0}, *e1 = NULL, *e2 = NULL;
        long long g1 = 0, g2 = 0;
        e2b_file_read("/big.bin", 3000000, 32, a, &g1, &e1);
        e2b_file_read("/copydst/big2.bin", 3000000, 32, b, &g2, &e2);
        expect("big copy content matches at 3MB offset",
               g1 == 32 && g2 == 32 && memcmp(a, b, 32) == 0);
    }

    /* 跨目录移动 + 同目录重命名 */
    expect("move /copydst/big2.bin -> /bigmoved.bin",
           e2b_rename("/copydst/big2.bin", "/bigmoved.bin", &err) == 0);
    expect("old path gone after move", e2b_stat("/copydst/big2.bin", &json, &err) != 0);
    expect("same-dir rename", e2b_rename("/bigmoved.bin", "/bigmoved2.bin", &err) == 0);
    expect("move conflict rejected",
           e2b_rename("/bigmoved2.bin", "/big.bin", &err) != 0);

    /* 目录移动（校验 ".." 修复）：/submoved/../docs 应可达 */
    expect("mkdir /copydst/sub", e2b_mkdir("/copydst/sub", &err) == 0);
    expect("move dir /copydst/sub -> /submoved",
           e2b_rename("/copydst/sub", "/submoved", &err) == 0);
    {
        char *st = NULL;
        int rc = e2b_stat("/submoved/../docs", &st, &err);
        json = call("dotdot points to new parent", rc, st, err);
        if (json) {
            e2b_free(json);
        }
        expect("dotdot target is root's docs", rc == 0);
    }
    /* 已移动目录的 ".." 现在指向根目录：/submoved/../big.bin 应可读 */
    {
        char buf[32] = {0}, *e2 = NULL;
        long long got = 0;
        int rc = e2b_file_read("/submoved/../big.bin", 0, 4, buf, &got, &e2);
        expect("read via moved dir dotdot", rc == 0 && got == 4);
        if (rc != 0) { printf("   err: %s\n", e2 ? e2 : "?"); e2b_free(e2); }
    }

    /* 拒绝把目录移动到自己的子树内 */
    expect("move dir into own subtree rejected",
           e2b_rename("/submoved", "/submoved/deep", &err) != 0);

    /* 删除：文件、递归目录 */
    expect("delete file /data.bin", e2b_delete("/data.bin", 1, &err) == 0);
    expect("deleted file gone", e2b_stat("/data.bin", &json, &err) != 0);
    expect("delete dir /nested (recursive)", e2b_delete("/nested", 1, &err) == 0);
    expect("deleted dir gone", e2b_stat("/nested", &json, &err) != 0);
    expect("non-recursive delete of non-empty dir rejected",
           e2b_delete("/copydst", 0, &err) != 0);

    expect("close", e2b_close() == 0);
    printf("\n%s (%d failures)\n", failures ? "WRITE TEST FAILED" : "ALL WRITE TESTS PASSED", failures);
    return failures ? 1 : 0;
}
