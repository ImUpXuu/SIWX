/*
 * wxhook.c — Linux 进程内 INT3 钩子：抓取微信 4.1.x 的 WCDB passphrase。
 *
 * 为什么不用 ptrace：微信 4.1.2x 内置 Chromium crashpad，会自己做
 * scoped_ptrace_attach 写 minidump；一个进程只能有一个 tracer，外部 tracer
 * 抢先附加后 crashpad 附加失败直接 abort()（特征日志
 * "scoped_ptrace_attach.cc:27 ptrace: Operation not permitted"）。
 * 所以本方案把断点打在**微信自己的进程内**，全程没有 ptrace，微信无感。
 *
 * 工作方式：
 *   1. 构造函数用 dl_iterate_phdr 取主程序 PIE 基址；
 *   2. 把 base+HOOK_OFFSET 首字节改成 INT3；
 *   3. 自己注册 SIGTRAP 处理器；**必须导出 sigaction 拦截符**，否则微信的
 *      crashpad 一旦安装自己的 SIGTRAP 处理器就会把我们的顶掉，INT3 落到
 *      对方手里 → 进程被当作致命信号杀掉（实测每次都是 code 5/SIGTRAP，
 *      RIP 恰为 bp+1）。另有看门狗每 50ms 复核，被顶掉就重装，装不回来
 *      就主动拆断点，绝不留一个没人接的 INT3；
 *   4. 命中后从 ucontext 的 REG_RSI 读结构体，passphrase 在 [rsi+8]，长度在 [rsi+16]；
 *   5. 复原字节并把 RIP 拨回断点（原指令照常执行），随即拆掉断点——一次性
 *      布点。早期版本会置 TF 单步后重新布点，实测与其它线程执行同一地址产生
 *      竞态，捕获后必崩（exe+0x87B20C4 SIGSEGV）。
 *
 * 构建（由 siwx.strategies.linux_hook 在运行时完成）：
 *   cc -shared -fPIC -O2 -DTARGET_EXE='"/opt/.../wechat"' -DHOOK_OFFSET=0x... \
 *      -o wxhook.so wxhook.c -ldl
 *
 * 注意：本文件经 LD_PRELOAD 注入（桌面启动器场景下由策略写入 systemd 用户
 * 管理器环境），可能波及同一窗口内新起的其它进程，因此构造函数第一件事就是
 * 校验 /proc/self/exe，不是目标微信进程立刻返回。
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <link.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

#ifndef HOOK_OFFSET
#error "HOOK_OFFSET must be provided by the build step"
#endif
#ifndef TARGET_EXE
#define TARGET_EXE ""
#endif

static unsigned long g_base;
static unsigned long g_bp;
static unsigned char g_orig_byte;
static volatile int g_armed;
static int g_fd = -1;

static struct sigaction g_app_act;
static volatile int g_have_app_act;
static int (*g_real_sigaction)(int, const struct sigaction *, struct sigaction *);
static struct sigaction g_mine;      /* 我们自己的 SIGTRAP 处理器 */
static volatile int g_rearm_fail;
/* 一次性布点：拿到一次就够了。命中后直接拆断点、不再单步重布，避免
 * "复原↔重布"与其它线程执行同一地址的竞态（实测重布点会在捕获后 SEGV）。 */
static volatile int g_done;


static void emit(const char *s, int n)
{
    if (g_fd >= 0) {
        ssize_t w = write(g_fd, s, (size_t)n);
        (void)w;
    }
}

static const char HEXD[] = "0123456789abcdef";

/* 手写十六进制，绝不在信号处理器里调 printf 族：snprintf 会碰 malloc/locale
 * 锁，一旦中断到正持锁的线程就污染堆，之后在无关位置 SEGV（实测踩过）。 */
static int append_hex(char *buf, int off, unsigned long v, int minwidth)
{
    char tmp[16];
    int n = 0;
    if (v == 0)
        tmp[n++] = '0';
    while (v) {
        tmp[n++] = HEXD[v & 0xf];
        v >>= 4;
    }
    while (n < minwidth)
        tmp[n++] = '0';
    while (n > 0)
        buf[off++] = tmp[--n];
    return off;
}

static void hexline(const char *tag, unsigned long a, unsigned long b,
                    const unsigned char *p, int n)
{
    char buf[256];
    int off = 0;
    while (*tag && off < 48)
        buf[off++] = *tag++;
    buf[off++] = ' ';
    buf[off++] = 'a';
    buf[off++] = '=';
    off = append_hex(buf, off, a, 1);
    buf[off++] = ' ';
    buf[off++] = 'b';
    buf[off++] = '=';
    off = append_hex(buf, off, b, 1);
    buf[off++] = ' ';
    const char *k = "data=";
    while (*k)
        buf[off++] = *k++;
    for (int i = 0; i < n && off < (int)sizeof buf - 2; i++) {
        buf[off++] = HEXD[(p[i] >> 4) & 0xf];
        buf[off++] = HEXD[p[i] & 0xf];
    }
    buf[off++] = '\n';
    emit(buf, off);
}

/* 读自身地址空间。目标不可读时返回 0 而不是把进程打死，且是纯 syscall，
 * 可以在信号处理器里安全调用（对野指针直接 memcpy 会 SEGV）。 */
static int safe_read(void *dst, unsigned long src, size_t n)
{
    struct iovec local = {.iov_base = dst, .iov_len = n};
    struct iovec remote = {.iov_base = (void *)src, .iov_len = n};
    long r = syscall(SYS_process_vm_readv, (long)getpid(),
                     &local, 1UL, &remote, 1UL, 0UL);
    return r == (long)n;
}

static void try_dump(const char *tag, unsigned long ptr, unsigned long len)
{
    unsigned char tmp[32];
    if (len != 32)
        return;
    if (!safe_read(tmp, ptr, sizeof tmp))
        return;
    hexline(tag, ptr, len, tmp, 32);
}

static void arming(int on)
{
    unsigned long page = g_bp & ~0xFFFUL;
    if (on) {
        unsigned char cur;
        memcpy(&cur, (void *)g_bp, 1);
        if (cur == 0xCC)
            return;
        g_orig_byte = cur;
        mprotect((void *)page, 0x1000, PROT_READ | PROT_WRITE | PROT_EXEC);
        *(volatile unsigned char *)g_bp = 0xCC;
        mprotect((void *)page, 0x1000, PROT_READ | PROT_EXEC);
        g_armed = 1;
    } else {
        mprotect((void *)page, 0x1000, PROT_READ | PROT_WRITE | PROT_EXEC);
        *(volatile unsigned char *)g_bp = g_orig_byte;
        mprotect((void *)page, 0x1000, PROT_READ | PROT_EXEC);
        g_armed = 0;
    }
}

static void chain_other(int sig, siginfo_t *si, void *uc)
{
    if (g_have_app_act) {
        if (g_app_act.sa_flags & SA_SIGINFO) {
            if (g_app_act.sa_sigaction)
                g_app_act.sa_sigaction(sig, si, uc);
            return;
        }
        if (g_app_act.sa_handler && g_app_act.sa_handler != SIG_DFL &&
            g_app_act.sa_handler != SIG_IGN) {
            g_app_act.sa_handler(sig);
            return;
        }
    }
    signal(sig, SIG_DFL);
    raise(sig);
}

static void on_sigtrap(int sig, siginfo_t *si, void *ucv)
{
    ucontext_t *uc = (ucontext_t *)ucv;
    greg_t *g = uc->uc_mcontext.gregs;

    unsigned long rip = (unsigned long)g[REG_RIP];
    if (rip - 1 != g_bp) {
        chain_other(sig, si, ucv);
        return;
    }

    unsigned long rsi = (unsigned long)g[REG_RSI];
    unsigned long rdx = (unsigned long)g[REG_RDX];

    /* 直接 32 字节入参 */
    try_dump("hit-raw", rsi, rdx);

    /* 结构体布局：{ptr, size, ...} 起于 +0 或 +8 */
    unsigned long hdr[3] = {0, 0, 0};
    if (safe_read(hdr, rsi, sizeof hdr)) {
        try_dump("hit-flat0", hdr[0], hdr[1]);
        try_dump("hit-flat8", hdr[1], hdr[2]);
    } else {
        emit("hit-unreadable\n", 14);
    }

    /* 复原原字节，并把 RIP 拨回断点，让原指令照常执行；不置 TF、不重布点。 */
    arming(0);
    g[REG_RIP] = g_bp;
    g_done = 1;
    emit("disarmed-one-shot\n", 18);
}

static int real_sigaction(int sig, const struct sigaction *act,
                          struct sigaction *old)
{
    if (!g_real_sigaction) {
        g_real_sigaction = (int (*)(int, const struct sigaction *,
                                    struct sigaction *))dlsym(RTLD_NEXT,
                                                              "sigaction");
        if (!g_real_sigaction)
            return -1;
    }
    return g_real_sigaction(sig, act, old);
}

/*
 * 导出的拦截符。此前这一层是 static 函数，符号没导出，等于没有任何保护：
 * 微信的 crashpad 一装自己的 SIGTRAP 处理器就把我们顶掉，INT3 落到对方手里，
 * 进程被当成致命信号杀掉。必须真正导出 sigaction 才会生效。
 */
int sigaction(int sig, const struct sigaction *act, struct sigaction *old)
{
    if (sig != SIGTRAP)
        return real_sigaction(sig, act, old);

    if (!act) /* 纯查询：报告我们的处理器，避免调用方以为被清空 */
        return real_sigaction(sig, &g_mine, old);

    if (g_mine.sa_sigaction == NULL) /* 构造函数尚未跑完，放行 */
        return real_sigaction(sig, act, old);

    /* 应用要装自己的 SIGTRAP 处理器：记下来（非我们的陷阱时串联过去），
     * 但实际装上的是我们的。 */
    if (act->sa_handler != SIG_DFL && act->sa_handler != SIG_IGN) {
        g_app_act = *act;
        g_have_app_act = 1;
    }
    struct sigaction prev;
    int r = real_sigaction(sig, &g_mine, &prev);
    if (old)
        *old = prev;
    return r;
}

/* glibc 内部调用走这两个符号，会绕过上面的 sigaction，一并接住。
 * glibc 的头文件给它们挂了 leaf/nothrow，我们的别名要调 dlsym，不能声明成
 * leaf，所以这里只压掉“属性不如目标严格”的告警。 */
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wmissing-attributes"
int __libc_sigaction(int sig, const struct sigaction *act, struct sigaction *old)
    __attribute__((alias("sigaction")));
int __sigaction(int sig, const struct sigaction *act, struct sigaction *old)
    __attribute__((alias("sigaction")));
#pragma GCC diagnostic pop

/* 看门狗：直调 syscall 的安装路径拦不住，每 50ms 复核一次。被顶掉就重装；
 * 复核三次仍不是我们的，说明这个进程没法安全保留断点，主动拆掉。 */
static void *watchdog_main(void *arg)
{
    (void)arg;
    for (;;) {
        if (g_armed && !g_done) {
            struct sigaction cur;
            if (real_sigaction(SIGTRAP, NULL, &cur) == 0) {
                int ours = (cur.sa_flags & SA_SIGINFO) &&
                           cur.sa_sigaction == on_sigtrap;
                if (!ours) {
                    if (cur.sa_handler != SIG_DFL && cur.sa_handler != SIG_IGN) {
                        g_app_act = cur;
                        g_have_app_act = 1;
                    }
                    real_sigaction(SIGTRAP, &g_mine, NULL);
                    emit("REARM\n", 6);
                    struct sigaction after;
                    if (real_sigaction(SIGTRAP, NULL, &after) == 0 &&
                        !((after.sa_flags & SA_SIGINFO) &&
                          after.sa_sigaction == on_sigtrap)) {
                        if (++g_rearm_fail >= 3) {
                            emit("DISARM handler-unavailable\n", 27);
                            arming(0);
                            g_rearm_fail = 0;
                        }
                    } else {
                        g_rearm_fail = 0;
                    }
                }
            }
        }
        struct timespec ts;
        ts.tv_sec = 0;
        ts.tv_nsec = 50L * 1000 * 1000;
        nanosleep(&ts, NULL);
    }
    return NULL;
}

static int is_target(void)
{
    char exe[512];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n <= 0)
        return 0;
    exe[n] = '\0';
    if (sizeof(TARGET_EXE) > 1)
        return strcmp(exe, TARGET_EXE) == 0;
    const char *slash = strrchr(exe, '/');
    return slash && strcmp(slash + 1, "wechat") == 0;
}

static int find_main(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    if (!info->dlpi_name || info->dlpi_name[0] == '\0') {
        *(unsigned long *)data = (unsigned long)info->dlpi_addr;
        return 1;
    }
    return 0;
}

__attribute__((constructor))
static void wxhook_init(void)
{
    /* /etc/ld.so.preload 会注入到系统里每一个进程，先确认是不是目标微信 */
    if (!is_target())
        return;

    const char *p = getenv("WXS_HOOK_LOG");
    g_fd = open(p ? p : "/tmp/siwx_linux_hook.log",
                O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (g_fd < 0)
        return;

    dl_iterate_phdr(find_main, &g_base);
    if (!g_base) {
        const char *e = "ERROR no-main-base\n";
        emit(e, (int)strlen(e));
        return;
    }
    g_bp = g_base + (unsigned long)HOOK_OFFSET;

    memset(&g_mine, 0, sizeof g_mine);
    g_mine.sa_sigaction = on_sigtrap;
    g_mine.sa_flags = SA_SIGINFO | SA_RESTART;
    sigfillset(&g_mine.sa_mask);
    /* 走 libc 原函数安装，别经过我们自己的拦截符（避免自我递归） */
    real_sigaction(SIGTRAP, &g_mine, NULL);

    arming(1);

    pthread_t th;
    if (pthread_create(&th, NULL, watchdog_main, NULL) == 0)
        pthread_detach(th);

    char msg[192];
    int n = snprintf(msg, sizeof msg, "INIT base=%lx bp=%lx armed=%d pid=%d\n",
                     g_base, g_bp, g_armed, (int)getpid());
    emit(msg, n);
}
