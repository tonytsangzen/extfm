/* bridge_test.c — 桥接层冒烟测试 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "bridge.h"

static void show(const char *what, int rc, char *json, char *err)
{
    printf("== %s ==\n", what);
    if (rc == 0) {
        printf("%s\n", json ? json : "(null)");
        e2b_free(json);
    } else {
        printf("FAILED: %s\n", err ? err : "(no message)");
        e2b_free(err);
    }
}

int main(int argc, char **argv)
{
    const char *img = argc > 1 ? argv[1] : "images/ext4.img";
    char *json = NULL, *err = NULL, *err2 = NULL;
    char buf[256];
    long long got = 0;
    int rc;

    printf("libext2fs version: %s\n", e2b_version());

    rc = e2b_open(img, &json, &err);
    show("open", rc, json, err);
    if (rc != 0)
        return 1;

    rc = e2b_list("/", &json, &err);
    show("list /", rc, json, err);

    rc = e2b_list("/docs", &json, &err);
    show("list /docs", rc, json, err);

    rc = e2b_stat("/docs", &json, &err);
    show("stat /docs", rc, json, err);

    rc = e2b_stat("/docs/说明.txt", &json, &err);
    show("stat /docs/说明.txt", rc, json, err);

    rc = e2b_file_read("/docs/说明.txt", 0, sizeof(buf) - 1, buf, &got, &err2);
    printf("== read /docs/说明.txt ==\n");
    if (rc == 0) {
        buf[got] = 0;
        printf("got %lld bytes: %s\n", got, buf);
    } else {
        printf("FAILED: %s\n", err2 ? err2 : "(no message)");
        e2b_free(err2);
    }

    /* 大文件偏移读取 */
    rc = e2b_file_read("/big.bin", 3000000, 64, buf, &got, &err2);
    printf("== read /big.bin @3000000 ==\n");
    if (rc == 0)
        printf("got %lld bytes (first=%02x)\n", got, (unsigned char)buf[0]);
    else {
        printf("FAILED: %s\n", err2 ? err2 : "(no message)");
        e2b_free(err2);
    }

    /* 探测：自身(ext) 与 非 ext 文件 */
    rc = e2b_probe(img, &json, &err);
    show("probe ext image", rc, json, err);

    rc = e2b_probe("/etc/hosts", &json, &err);
    show("probe non-ext file", rc, json, err);

    rc = e2b_probe("/nonexistent/path/img", &json, &err);
    show("probe missing path", rc, json, err);

    /* 错误路径 */
    rc = e2b_list("/no/such/dir", &json, &err);
    show("list missing dir (expect FAIL)", rc, json, err);

    e2b_close();
    printf("ALL DONE\n");
    return 0;
}
